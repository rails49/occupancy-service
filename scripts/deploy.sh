#!/usr/bin/env bash
#
# Brings this stack up on a box, built there and named by the commit it was
# built from.
#
#   scripts/deploy.sh <user>@<box> [<branch>]
#
# One ssh, to the clone at ~/occupancy-service on the box. It stops on a box
# with no /etc/rails49/occupancy.env, brings the clone to the head of the
# branch, main by default, refuses a clone that is not clean, and builds and
# starts both images under the commit, with the settings in box.env and
# occupancy.env.
set -euo pipefail

box=${1:?usage: scripts/deploy.sh <user>@<box> [<branch>]}
branch=${2:-main}

# The arguments are quoted here, for the box's shell.
# shellcheck disable=SC2029
ssh "$box" "bash -l -s -- $(printf '%q ' "$branch")" <<'REMOTE'
set -euo pipefail
branch=$1

if [ ! -f /etc/rails49/occupancy.env ]; then
  echo "this box has no /etc/rails49/occupancy.env, which holds the stack's" >&2
  echo "settings; write it as the README's \"Box\" section says." >&2
  exit 1
fi

# The heredoc is this shell's stdin, so a credential prompt would read the rest
# of the script as its answer.
export GIT_TERMINAL_PROMPT=0
cd ~/occupancy-service
git remote set-url origin https://github.com/rails49/occupancy-service.git
git fetch --prune origin
git checkout "$branch"
git merge --ff-only "origin/$branch"

if [ -n "$(git status --porcelain)" ]; then
  echo "the clone at ~/occupancy-service is not clean, and its images would be" >&2
  echo "named by a commit that is not what was built:" >&2
  git status --short >&2
  exit 1
fi

OCCUPANCY_SERVICE_COMMIT=$(git rev-parse HEAD)
export OCCUPANCY_SERVICE_COMMIT

docker compose --env-file /etc/rails49/box.env --env-file /etc/rails49/occupancy.env \
  -f compose.yaml up -d --build
echo "deployed occupancy-service:$OCCUPANCY_SERVICE_COMMIT"
REMOTE
