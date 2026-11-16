#!/usr/bin/env bash
# Déploie ou met à jour le serveur memex depuis une machine de travail.
#
#   server/deploy.sh root@mon-vps [options de server/install.sh]
#
# Envoie la version commitée du dépôt (git archive HEAD), puis lance
# l'installeur sur place. Rejouable : c'est aussi la commande de mise à jour.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

[ $# -ge 1 ] || die "usage : server/deploy.sh UTILISATEUR@HÔTE [options de server/install.sh]"
TARGET="$1"; shift
need git
need ssh

if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    warn "le dépôt contient des modifications non commitées : elles ne seront pas déployées."
fi

REMOTE_DIR="/opt/memex/src"
ARGS=""
for arg in "$@"; do ARGS="$ARGS $(printf '%q' "$arg")"; done

step "Envoi de $(git -C "$REPO" rev-parse --short HEAD) vers $TARGET:$REMOTE_DIR"
git -C "$REPO" archive --format=tar HEAD | ssh "$TARGET" "
    set -e
    SUDO=''; [ \"\$(id -u)\" = 0 ] || SUDO='sudo'
    \$SUDO rm -rf $REMOTE_DIR && \$SUDO mkdir -p $REMOTE_DIR && \$SUDO tar -x -C $REMOTE_DIR
"

step "Installation sur le serveur"
# shellcheck disable=SC2029
ssh -t "$TARGET" "
    SUDO=''; [ \"\$(id -u)\" = 0 ] || SUDO='sudo'
    \$SUDO bash $REMOTE_DIR/server/install.sh $ARGS
"
