#!/bin/bash

export GITHUB_TOKEN=ghp_xxx     # org owner; classic PAT needs repo + admin:org

python3 provision_repos.py \
    --org Chem281 \
    --template-owner Chem281Materials \
    --template-repo 4-1_memorymap \
    --roster roster.csv \
    --prefix 4-1 \
    --staff-team "Course Staff" \
    --staff-members taylor-a-barnes
#    --dry-run
