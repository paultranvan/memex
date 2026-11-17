#!/usr/bin/env bash
# Deploys or updates the memex server from a work machine.
#
#   server/deploy.sh root@my-server [server/install.sh options]
#
# Sends the committed version of the repository (git archive HEAD), then runs
# the installer there. Safe to run again: it is also the update command.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

[ $# -ge 1 ] || die "usage: server/deploy.sh USER@HOST [server/install.sh options]"
TARGET="$1"; shift
need git
need ssh

if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    warn "the repository has uncommitted changes: they will not be deployed"
fi

REMOTE_DIR="/opt/memex/src"
ARGS=""
for arg in "$@"; do ARGS="$ARGS $(printf '%q' "$arg")"; done

step "Sending $(git -C "$REPO" rev-parse --short HEAD) to $TARGET:$REMOTE_DIR"
git -C "$REPO" archive --format=tar HEAD | ssh "$TARGET" "
    set -e
    SUDO=''; [ \"\$(id -u)\" = 0 ] || SUDO='sudo'
    \$SUDO rm -rf $REMOTE_DIR && \$SUDO mkdir -p $REMOTE_DIR && \$SUDO tar -x -C $REMOTE_DIR
"

step "Installing on the server"
# shellcheck disable=SC2029
ssh -t "$TARGET" "
    SUDO=''; [ \"\$(id -u)\" = 0 ] || SUDO='sudo'
    \$SUDO bash $REMOTE_DIR/server/install.sh $ARGS
"
