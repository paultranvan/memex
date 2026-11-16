#!/usr/bin/env bash
# Retire le serveur memex : minuteries, unités, programme. Les données (wiki,
# condensés, sauvegardes) et la configuration sont conservées, sauf avec --purge.
# À lancer en root, ou avec --user-mode pour une installation faite sans root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

MEMEX_HOME_DIR="/srv/memex" MEMEX_USER="memex" USER_MODE=0 PURGE=0

usage() {
    cat <<'EOF'
Usage : server/uninstall.sh [options]

  --home CHEMIN   racine de memex sur ce serveur (défaut : /srv/memex)
  --user NOM      compte système de memex (défaut : memex)
  --user-mode     installation faite sans root, sous le compte courant
  --purge         supprimer aussi les données : wiki, condensés, sauvegardes
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --home) MEMEX_HOME_DIR="$2"; shift 2 ;;
        --user) MEMEX_USER="$2"; shift 2 ;;
        --user-mode) USER_MODE=1; shift ;;
        --purge) PURGE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "option inconnue : $1" ;;
    esac
done

if [ "$USER_MODE" = "1" ]; then
    [ "$MEMEX_HOME_DIR" = "/srv/memex" ] && MEMEX_HOME_DIR="$HOME/.local/share/memex-server"
    APP="$MEMEX_HOME_DIR/app"
    BIN_DIR="$HOME/.local/bin"
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/memex"
    UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    SKILL="$HOME/.hermes/skills/memex"
    sysctl_() { systemctl --user "$@"; }
else
    [ "$(id -u)" = "0" ] || die "lance ce script en root (sudo), ou ajoute --user-mode."
    APP="/opt/memex/app"
    BIN_DIR="/usr/local/bin"
    CONFIG_DIR="/etc/memex"
    UNITS="/etc/systemd/system"
    SKILL="$MEMEX_HOME_DIR/.hermes/skills/memex"
    sysctl_() { systemctl "$@"; }
fi

step "Minuteries et unités"
UNIT_NAMES="memex-run.path memex-run.timer memex-verif.timer memex-backup.timer memex-run.service memex-verif.service memex-backup.service"
if command -v systemctl >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    sysctl_ disable --now $UNIT_NAMES >/dev/null 2>&1 || true
fi
for unit in $UNIT_NAMES; do rm -f "$UNITS/$unit"; done
if command -v systemctl >/dev/null 2>&1; then sysctl_ daemon-reload >/dev/null 2>&1 || true; fi
say "  arrêtées et retirées"

step "Programme"
# La commande « memex » peut appartenir à la partie hôte de la même machine : on ne
# retire que celle qui pointe vers le programme du serveur.
for name in memex memex-gate; do
    if [ -f "$BIN_DIR/$name" ] && grep -q -F "PYTHONPATH=\"$APP" "$BIN_DIR/$name"; then
        rm -f "$BIN_DIR/$name"
        say "  $BIN_DIR/$name retirée"
    fi
done
rm -rf "$APP"
[ "$USER_MODE" = "0" ] && rmdir /opt/memex 2>/dev/null || true
rm -rf "$SKILL"

if [ "$PURGE" = "1" ]; then
    step "Données"
    case "$MEMEX_HOME_DIR" in
        ""|/|/home|/root|/srv|/var|/opt|"$HOME") die "refus de supprimer « $MEMEX_HOME_DIR »." ;;
    esac
    if [ -d "$MEMEX_HOME_DIR" ]; then
        # Garde-fou : on ne supprime qu'un dossier qui ressemble à une installation de memex.
        [ -d "$MEMEX_HOME_DIR/wiki/.git" ] || [ -d "$MEMEX_HOME_DIR/raw" ] || [ -d "$MEMEX_HOME_DIR/state" ] \
            || die "« $MEMEX_HOME_DIR » ne ressemble pas à un dossier de memex : rien n'est supprimé."
        rm -rf "$MEMEX_HOME_DIR"
        say "  $MEMEX_HOME_DIR supprimé (wiki, condensés, sauvegardes)"
    fi
    if [ "$USER_MODE" = "0" ]; then
        rm -rf "$CONFIG_DIR"
        say "  $CONFIG_DIR supprimé"
        say "  Le compte « $MEMEX_USER » est laissé en place. Pour le retirer :  userdel $MEMEX_USER"
    else
        say "  La configuration ($CONFIG_DIR) est partagée avec la partie hôte : host/uninstall.sh --purge la retire."
    fi
    say "  Le dossier du vault (MEMEX_VAULT_DIR), s'il est ailleurs, n'est jamais supprimé."
else
    say ""
    say "Conservés : $MEMEX_HOME_DIR (wiki, condensés, sauvegardes) et la configuration."
    say "Réinstaller : server/install.sh. Tout supprimer : server/uninstall.sh --purge"
fi
