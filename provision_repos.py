#!/usr/bin/env python3
"""
provision_repos.py

Provision per-student repositories from a GitHub template repository, replacing
a small subset of GitHub Classroom functionality.

For each student in a roster CSV it will:
  1. Create a repository from a template repo, inside a target organization.
  2. Mark that repository private.
  3. Grant the student write (push) access.
  4. Grant a staff team admin access (instructors / TAs).

The script is idempotent: re-running it skips repositories that already exist
and re-applies access grants, so you can run it repeatedly as students enroll.

------------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------------
  export GITHUB_TOKEN=ghp_xxx            # org owner; needs repo + admin:org

  python provision_repos.py \
      --org my-course-org \
      --template-owner my-course-org \
      --template-repo assignment1-template \
      --roster roster.csv \
      --prefix assignment1 \
      --staff-team course-staff \
      --staff-members prof_alice ta_bob ta_carol

Add --dry-run to see what would happen without making any changes.

------------------------------------------------------------------------------
ROSTER FORMAT (CSV with a header row)
------------------------------------------------------------------------------
  identifier,github_username
  jdoe,janedoe-gh
  msmith,msmith42

  - "identifier" is your internal id (student number, SIS login, etc.). It is
    used only to name the repo, so it can be anything filename-safe.
  - "github_username" is the student's GitHub login. Required.

Repository names are formed as:  {prefix}-{identifier}
e.g.  assignment1-jdoe
(Use --name-by username to name them {prefix}-{github_username} instead.)

------------------------------------------------------------------------------
TOKEN / PERMISSIONS
------------------------------------------------------------------------------
The token owner must be an organization owner (or hold equivalent org perms).
  - Classic PAT: scopes  repo  +  admin:org
  - Fine-grained PAT / GitHub App scoped to the org: Administration (read/write)
    on repositories, Members (read/write) on the org.

------------------------------------------------------------------------------
NOTE ON INVITATIONS
------------------------------------------------------------------------------
If a student is NOT already a member of the organization, adding them as a
collaborator sends an email invitation that THEY must accept; there is no API
way to accept on their behalf. This script reports when an invitation is
pending so you can track who still needs to accept.
"""

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

# How many times to retry a call that hits a secondary rate limit / 5xx.
MAX_RETRIES = 5
# Small courtesy delay between repo creations to stay under the secondary limit.
CREATE_PAUSE_SECONDS = 1.0


# --------------------------------------------------------------------------- #
# HTTP helper with retry/backoff for secondary rate limits and transient errors
# --------------------------------------------------------------------------- #
class GitHub:
    def __init__(self, token: str, dry_run: bool = False):
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "provision-repos-script",
            }
        )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.request(method, url, **kwargs)

            # Secondary rate limit or abuse detection -> back off and retry.
            if resp.status_code in (403, 429):
                retry_after = resp.headers.get("Retry-After")
                body = resp.text.lower()
                is_secondary = (
                    retry_after is not None
                    or "secondary rate limit" in body
                    or "abuse" in body
                )
                if is_secondary and attempt < MAX_RETRIES:
                    wait = int(retry_after) if retry_after else min(2 ** attempt, 60)
                    print(f"    rate limited; sleeping {wait}s "
                          f"(attempt {attempt}/{MAX_RETRIES})", file=sys.stderr)
                    time.sleep(wait)
                    continue

            # Transient server errors -> retry with backoff.
            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
                wait = min(2 ** attempt, 60)
                print(f"    server error {resp.status_code}; sleeping {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
                continue

            return resp
        return resp  # last response, even if not ideal

    def get(self, path: str, **kw) -> requests.Response:
        return self._request("GET", path, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self._request("POST", path, **kw)

    def put(self, path: str, **kw) -> requests.Response:
        return self._request("PUT", path, **kw)


# --------------------------------------------------------------------------- #
# Domain operations
# --------------------------------------------------------------------------- #
@dataclass
class Student:
    identifier: str
    username: str


def load_roster(path: str) -> list[Student]:
    students: list[Student] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise SystemExit(f"Roster {path!r} is empty or has no header row.")
        cols = {c.strip().lower() for c in reader.fieldnames}
        if "github_username" not in cols:
            raise SystemExit(
                "Roster must have a 'github_username' column. "
                f"Found columns: {sorted(cols)}"
            )
        has_id = "identifier" in cols
        for i, row in enumerate(reader, start=2):  # line 2 = first data row
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            username = row.get("github_username", "")
            if not username:
                print(f"  skipping roster line {i}: empty github_username",
                      file=sys.stderr)
                continue
            identifier = row.get("identifier") if has_id else ""
            students.append(Student(identifier=identifier or username,
                                    username=username))
    return students


def repo_exists(gh: GitHub, org: str, name: str) -> bool:
    resp = gh.get(f"/repos/{org}/{name}")
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise RuntimeError(f"Unexpected status checking repo {org}/{name}: "
                       f"{resp.status_code} {resp.text}")


def create_repo_from_template(gh: GitHub, template_owner: str, template_repo: str,
                              org: str, name: str) -> None:
    if gh.dry_run:
        print(f"  [dry-run] would create {org}/{name} from "
              f"{template_owner}/{template_repo} (private)")
        return
    resp = gh.post(
        f"/repos/{template_owner}/{template_repo}/generate",
        json={
            "owner": org,
            "name": name,
            "private": True,
            "include_all_branches": False,
        },
    )
    if resp.status_code in (201,):
        print(f"  created {org}/{name} (private)")
    elif resp.status_code == 422 and "already exists" in resp.text.lower():
        print(f"  {org}/{name} already exists; leaving as-is")
    else:
        raise RuntimeError(f"Failed to create {org}/{name}: "
                           f"{resp.status_code} {resp.text}")
    time.sleep(CREATE_PAUSE_SECONDS)


def add_collaborator(gh: GitHub, org: str, repo: str, username: str,
                     permission: str = "push") -> None:
    if gh.dry_run:
        print(f"  [dry-run] would grant {username} '{permission}' on {org}/{repo}")
        return
    resp = gh.put(
        f"/repos/{org}/{repo}/collaborators/{username}",
        json={"permission": permission},
    )
    if resp.status_code == 201:
        print(f"  invited {username} ({permission}) -> INVITATION PENDING "
              f"(student must accept)")
    elif resp.status_code == 204:
        print(f"  {username} already has access; ensured '{permission}'")
    elif resp.status_code == 200:
        print(f"  granted {username} '{permission}'")
    else:
        raise RuntimeError(f"Failed to add {username} to {org}/{repo}: "
                           f"{resp.status_code} {resp.text}")


def ensure_team(gh: GitHub, org: str, team_name: str,
                members: list[str]) -> str:
    """Return the team slug, creating the team and adding members if needed."""
    slug = team_name.lower().replace(" ", "-")
    if gh.dry_run:
        print(f"  [dry-run] would ensure team '{team_name}' (slug: {slug}) in {org}")
        for member in members:
            print(f"  [dry-run] would add {member} to team '{team_name}' (maintainer)")
        return slug
    resp = gh.get(f"/orgs/{org}/teams/{slug}")
    if resp.status_code == 404:
        create = gh.post(
            f"/orgs/{org}/teams",
            json={"name": team_name, "privacy": "closed"},
        )
        if create.status_code != 201:
            raise RuntimeError(f"Failed to create team {team_name}: "
                               f"{create.status_code} {create.text}")
        slug = create.json()["slug"]
        print(f"  created team '{team_name}' (slug: {slug})")
    elif resp.status_code == 200:
        slug = resp.json()["slug"]
    else:
        raise RuntimeError(f"Unexpected status checking team {slug}: "
                           f"{resp.status_code} {resp.text}")

    for member in members:
        if gh.dry_run:
            print(f"  [dry-run] would add {member} to team '{team_name}' (maintainer)")
            continue
        m = gh.put(
            f"/orgs/{org}/teams/{slug}/memberships/{member}",
            json={"role": "maintainer"},
        )
        if m.status_code == 200:
            state = m.json().get("state", "")
            note = " (invitation pending)" if state == "pending" else ""
            print(f"  staff member {member} added to team{note}")
        else:
            print(f"  WARNING: could not add staff {member}: "
                  f"{m.status_code} {m.text}", file=sys.stderr)
    return slug


def grant_team_admin(gh: GitHub, org: str, team_slug: str, repo: str) -> None:
    if gh.dry_run:
        print(f"  [dry-run] would grant team '{team_slug}' admin on {org}/{repo}")
        return
    resp = gh.put(
        f"/orgs/{org}/teams/{team_slug}/repos/{org}/{repo}",
        json={"permission": "admin"},
    )
    if resp.status_code == 204:
        print(f"  staff team '{team_slug}' granted admin on {org}/{repo}")
    else:
        raise RuntimeError(f"Failed to grant team admin on {org}/{repo}: "
                           f"{resp.status_code} {resp.text}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Provision per-student repos from a template (mini GitHub "
                    "Classroom).")
    p.add_argument("--org", required=True, help="Target organization.")
    p.add_argument("--template-owner", required=True,
                   help="Owner of the template repo (often the same org).")
    p.add_argument("--template-repo", required=True,
                   help="Template repository name (must be a template repo).")
    p.add_argument("--roster", required=True, help="Path to roster CSV.")
    p.add_argument("--prefix", required=True,
                   help="Repo name prefix, e.g. 'assignment1'.")
    p.add_argument("--staff-team", required=True,
                   help="Name of the staff team to grant admin.")
    p.add_argument("--staff-members", nargs="*", default=[],
                   help="GitHub usernames of instructors/TAs to add to the team.")
    p.add_argument("--name-by", choices=["identifier", "username"],
                   default="identifier",
                   help="Whether repo names use the roster identifier or the "
                        "GitHub username (default: identifier).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show actions without making changes.")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set the GITHUB_TOKEN environment variable.", file=sys.stderr)
        return 2

    students = load_roster(args.roster)
    if not students:
        print("No students found in roster; nothing to do.", file=sys.stderr)
        return 1

    gh = GitHub(token, dry_run=args.dry_run)

    print(f"Ensuring staff team '{args.staff_team}'...")
    team_slug = ensure_team(gh, args.org, args.staff_team, args.staff_members)
    print()

    failures: list[tuple[str, str]] = []
    print(f"Provisioning {len(students)} repositories"
          f"{' (dry-run)' if args.dry_run else ''}...")
    for s in students:
        key = s.identifier if args.name_by == "identifier" else s.username
        repo = f"{args.prefix}-{key}"
        print(f"- {repo}  (student: {s.username})")
        try:
            if args.dry_run or not repo_exists(gh, args.org, repo):
                create_repo_from_template(
                    gh, args.template_owner, args.template_repo, args.org, repo)
            else:
                print(f"  {args.org}/{repo} already exists; ensuring access only")
            add_collaborator(gh, args.org, repo, s.username, permission="push")
            grant_team_admin(gh, args.org, team_slug, repo)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  ERROR: {exc}", file=sys.stderr)
            failures.append((repo, str(exc)))

    print()
    done = len(students) - len(failures)
    print(f"Done. {done}/{len(students)} repositories provisioned successfully.")
    if failures:
        print("Failures:", file=sys.stderr)
        for repo, err in failures:
            print(f"  {repo}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
