#!/usr/bin/env bash
# Removes the memex server: timers, units, program. The data (wiki, segments,
# backups) and the configuration are kept, unless --purge is given.
# Run it as root, or with --user-mode for an installation made without root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

MEMEX_HOME_DIR="/srv/memex" MEMEX_USER="memex" USER_MODE=0 PURGE=0

usage() {
    cat <<'EOF'
Usage: server/uninstall.sh [options]

  --home PATH     root of memex on this server (default: /srv/memex)
  --user NAME     system account of memex (default: memex)
  --user-mode     installation made without root, under the current account
  --purge         also delete the data: wiki, segments, backups
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --home) MEMEX_HOME_DIR="$2"; shift 2 ;;
        --user) MEMEX_USER="$2"; shift 2 ;;
        --user-mode) USER_MODE=1; shift ;;
        --purge) PURGE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
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
    [ "$(id -u)" = "0" ] || die "run this script as root (sudo), or add --user-mode"
    APP="/opt/memex/app"
    BIN_DIR="/usr/local/bin"
    CONFIG_DIR="/etc/memex"
    UNITS="/etc/systemd/system"
    SKILL="$MEMEX_HOME_DIR/.hermes/skills/memex"
    sysctl_() { systemctl "$@"; }
fi

step "Timers and units"
UNIT_NAMES="memex-run.path memex-run.timer memex-curate.timer memex-backup.timer memex-run.service memex-curate.service memex-backup.service"
if command -v systemctl >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    sysctl_ disable --now $UNIT_NAMES >/dev/null 2>&1 || true
fi
for unit in $UNIT_NAMES; do rm -f "$UNITS/$unit"; done
if command -v systemctl >/dev/null 2>&1; then sysctl_ daemon-reload >/dev/null 2>&1 || true; fi
say "  stopped and removed"

step "Program"
# The "memex" command may belong to the host side of the same machine: only the
# one that points to the server's program is removed.
for name in memex memex-gate; do
    if [ -f "$BIN_DIR/$name" ] && grep -q -F "PYTHONPATH=\"$APP" "$BIN_DIR/$name"; then
        rm -f "$BIN_DIR/$name"
        say "  $BIN_DIR/$name removed"
    fi
done
rm -rf "$APP"
[ "$USER_MODE" = "0" ] && rmdir /opt/memex 2>/dev/null || true
rm -rf "$SKILL"

if [ "$PURGE" = "1" ]; then
    step "Data"
    case "$MEMEX_HOME_DIR" in
        ""|/|/home|/root|/srv|/var|/opt|"$HOME") die "refusing to delete \"$MEMEX_HOME_DIR\"" ;;
    esac
    if [ -d "$MEMEX_HOME_DIR" ]; then
        # Guardrail: only a directory that looks like a memex installation is deleted.
        [ -d "$MEMEX_HOME_DIR/wiki/.git" ] || [ -d "$MEMEX_HOME_DIR/raw" ] || [ -d "$MEMEX_HOME_DIR/state" ] \
            || die "\"$MEMEX_HOME_DIR\" does not look like a memex directory: nothing was deleted"
        rm -rf "$MEMEX_HOME_DIR"
        say "  $MEMEX_HOME_DIR deleted (wiki, segments, backups)"
    fi
    if [ "$USER_MODE" = "0" ]; then
        rm -rf "$CONFIG_DIR"
        say "  $CONFIG_DIR deleted"
        say "  The \"$MEMEX_USER\" account is left in place. To remove it:  userdel $MEMEX_USER"
    else
        say "  The configuration ($CONFIG_DIR) is shared with the host side: host/uninstall.sh --purge removes it."
    fi
    say "  The vault directory (MEMEX_VAULT_DIR), if it is elsewhere, is never deleted."
else
    say ""
    say "Kept: $MEMEX_HOME_DIR (wiki, segments, backups) and the configuration."
    say "To reinstall: server/install.sh. To delete everything: server/uninstall.sh --purge"
fi
