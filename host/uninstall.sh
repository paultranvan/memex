#!/usr/bin/env bash
# Retire memex d'une machine de travail. Les données (condensés envoyés, copie
# du wiki, configuration) sont conservées, sauf avec --purge.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

DATA="${MEMEX_DATA_DIR:-$HOME/.local/share/memex}"
BIN="$HOME/.local/bin/memex"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

step "Minuterie"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now memex-sync.timer >/dev/null 2>&1 || true
    rm -f "$UNITS/memex-sync.service" "$UNITS/memex-sync.timer"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

step "Branchements Claude Code et Codex"
if [ -x "$BIN" ]; then "$BIN" integrate remove | sed 's/^/  /' || true; fi

step "Programme"
rm -rf "$DATA/app" "$BIN"

if [ "$PURGE" = "1" ]; then
    step "Données et configuration"
    rm -rf "$DATA" "${XDG_CONFIG_HOME:-$HOME/.config}/memex"
    say "  supprimées (la clé ~/.ssh/memex_ed25519 est laissée en place)"
else
    say ""
    say "Conservés : $DATA (condensés, copie du wiki) et la configuration."
    say "Pour tout supprimer : host/uninstall.sh --purge"
fi
