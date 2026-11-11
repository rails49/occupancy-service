#!/usr/bin/env bash
#
# Brings this stack up on a box, built there and named by the commit it was
# built from.
#
#   OCCUPANCY_ARCHIVE=/srv/rails49/layout.r49 \
#   OCCUPANCY_CAMERA=/dev/v4l/by-id/usb-<camera>-video-index0 \
#   scripts/deploy.sh <user>@<box> [<branch>]
#
# One ssh, to the clone at ~/occupancy-service on the box. It brings the clone
# to the head of the branch, main by default, and refuses a clone that is not
# clean: its images would be named by a commit that is not what was built.
set -euo pipefail

box=${1:?usage: scripts/deploy.sh <user>@<box> [<branch>]}
branch=${2:-main}
archive=${OCCUPANCY_ARCHIVE:?the archive\'s path on the box}
camera=${OCCUPANCY_CAMERA:?the camera\'s path on the box, under /dev/v4l/by-id/}

# The arguments are quoted here, for the box's shell.
# shellcheck disable=SC2029
ssh "$box" "bash -l -s -- $(printf '%q ' "$branch" "$archive" "$camera")" <<'REMOTE'
set -euo pipefail
branch=$1

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
TC49_UID=$(id -u)
TC49_GID=$(id -g)
VIDEO_GID=$(getent group video | cut -d: -f3)
RENDER_GID=$(getent group render | cut -d: -f3)
export OCCUPANCY_SERVICE_COMMIT TC49_UID TC49_GID VIDEO_GID RENDER_GID
export OCCUPANCY_ARCHIVE=$2 OCCUPANCY_CAMERA=$3

docker compose --env-file /etc/rails49/box.env -f compose.yaml up -d --build
echo "deployed occupancy-service:$OCCUPANCY_SERVICE_COMMIT"
REMOTE
