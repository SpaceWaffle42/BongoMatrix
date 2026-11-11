#!/bin/sh
# Rebuild the prod_device branch from this commit: the same files minus the development-only ones
# (tools/prod_tree.py). Each prod commit sits on the previous prod commit, so devices always
# fast-forward and the branch's history is only ever display files. Run by CI on main.
set -eu
: "${PROD_PUSH_URL:?set PROD_PUSH_URL (https://oauth2:TOKEN@host/group/project.git)}"
git config user.name "Bongo Matrix CI"
git config user.email "ci@bongo-matrix.invalid"
parent=""
if git fetch -q "$PROD_PUSH_URL" refs/heads/prod_device 2>/dev/null; then
  parent=$(git rev-parse FETCH_HEAD)
fi
python tools/prod_tree.py --apply
tree=$(git write-tree)
if [ -n "$parent" ] && [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
  echo "prod_device already matches $(git rev-parse --short HEAD)"; exit 0
fi
msg="prod_device from $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
if [ -n "$parent" ]; then new=$(git commit-tree "$tree" -p "$parent" -m "$msg"); else new=$(git commit-tree "$tree" -m "$msg"); fi
git push -q "$PROD_PUSH_URL" "$new:refs/heads/prod_device"
echo "prod_device -> $(git rev-parse --short "$new")"
