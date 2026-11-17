#!/usr/bin/env bash
# Removes memex from a work machine. The data (segments already sent, copy of
# the wiki, configuration) is kept, unless --purge is given.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

DATA="${MEMEX_DATA_DIR:-$HOME/.local/share/memex}"
BIN="$HOME/.local/bin/memex"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

step "Timer"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now memex-sync.timer >/dev/null 2>&1 || true
    rm -f "$UNITS/memex-sync.service" "$UNITS/memex-sync.timer"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

step "Integration with Claude Code and Codex"
if [ -x "$BIN" ]; then "$BIN" integrate remove | sed 's/^/  /' || true; fi

step "Program"
rm -rf "$DATA/app" "$BIN"

if [ "$PURGE" = "1" ]; then
    step "Data and configuration"
    rm -rf "$DATA" "${XDG_CONFIG_HOME:-$HOME/.config}/memex"
    say "  deleted (the key ~/.ssh/memex_ed25519 is left in place)"
else
    say ""
    say "Kept: $DATA (segments, copy of the wiki) and the configuration."
    say "To delete everything: host/uninstall.sh --purge"
fi
