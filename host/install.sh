#!/usr/bin/env bash
# Installs memex on a work machine (Claude Code, Codex).
# Safe to run again: each step checks what is already in place.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

SERVER="" SERVER_HOME="" DEVICE="" SOURCES="" EXCLUDE_PATHS="" EXCLUDE_REMOTES=""
TIMER=1 INTEGRATE=1 LINK=0 KEY=1 INTERVAL="10min" BACKFILL=1

usage() {
    cat <<'EOF'
Usage: host/install.sh [options]

  --server USER@HOST          SSH address of the memex server ("local": the server is on this machine)
  --server-home PATH          value of MEMEX_HOME on the server (default: /srv/memex)
  --device NAME               name of this machine (default: its hostname)
  --sources LIST              sources to capture (default: claude-code,codex)
  --exclude-paths LIST        directories never to capture
  --exclude-remotes LIST      git repositories never to capture (patterns)
  --interval DURATION         how often to synchronise (default: 10min)
  --no-backfill               skip the backfill: only future sessions are captured
  --no-timer                  do not install the timer
  --no-integrate              leave the configuration of Claude Code and Codex untouched
  --no-key                    do not create a dedicated SSH key
  --link                      link the repository instead of copying it (development)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --server-home) SERVER_HOME="$2"; shift 2 ;;
        --device) DEVICE="$2"; shift 2 ;;
        --sources) SOURCES="$2"; shift 2 ;;
        --exclude-paths) EXCLUDE_PATHS="$2"; shift 2 ;;
        --exclude-remotes) EXCLUDE_REMOTES="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --no-backfill) BACKFILL=0; shift ;;
        --no-timer) TIMER=0; shift ;;
        --no-integrate) INTEGRATE=0; shift ;;
        --no-key) KEY=0; shift ;;
        --link) LINK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

step "Checks"
check_python
need git
need ssh

DATA="${MEMEX_DATA_DIR:-$HOME/.local/share/memex}"
APP="$DATA/app"
BIN="$HOME/.local/bin/memex"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/memex"
CONFIG="$CONFIG_DIR/memex.env"

step "Installing the program in $APP"
if [ "$LINK" = "1" ]; then install_app "$REPO/src" "$APP" 1; else install_app "$REPO/src" "$APP"; fi
write_wrapper "$BIN" "$APP"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "$HOME/.local/bin is not in your PATH: add it to run \"memex\" directly" ;;
esac

step "Configuration: $CONFIG"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
    cp "$REPO/config/memex.env.example" "$CONFIG"
    chmod 600 "$CONFIG"
    say "  created from the template"
else
    say "  already present: only the options given on the command line are changed"
fi
[ -n "$SERVER" ] && set_kv "$CONFIG" MEMEX_SERVER_SSH "$SERVER"
[ -n "$SERVER_HOME" ] && set_kv "$CONFIG" MEMEX_SERVER_HOME "$SERVER_HOME"
[ -n "$DEVICE" ] && set_kv "$CONFIG" MEMEX_DEVICE "$DEVICE"
[ -n "$SOURCES" ] && set_kv "$CONFIG" MEMEX_SOURCES "$SOURCES"
[ -n "$EXCLUDE_PATHS" ] && set_kv "$CONFIG" MEMEX_EXCLUDE_PATHS "$EXCLUDE_PATHS"
[ -n "$EXCLUDE_REMOTES" ] && set_kv "$CONFIG" MEMEX_EXCLUDE_REMOTES "$EXCLUDE_REMOTES"

CURRENT_SERVER="$(sed -n 's/^MEMEX_SERVER_SSH=//p' "$CONFIG" | tail -1 | tr -d '"'"'")"
PUBKEY=""
if [ "$KEY" = "1" ] && [ -n "$CURRENT_SERVER" ] && [ "$CURRENT_SERVER" != "local" ]; then
    step "SSH key dedicated to memex"
    KEYFILE="$HOME/.ssh/memex_ed25519"
    if [ ! -f "$KEYFILE" ]; then
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        ssh-keygen -q -t ed25519 -N "" -C "memex@$(hostname -s 2>/dev/null || hostname)" -f "$KEYFILE"
        say "  created: $KEYFILE"
    else
        say "  already present: $KEYFILE"
    fi
    set_kv "$CONFIG" MEMEX_SSH_OPTS "-i $KEYFILE -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=6"
    PUBKEY="$(cat "$KEYFILE.pub")"
fi

if [ "$BACKFILL" = "0" ] && [ ! -f "$DATA/state.json" ]; then
    step "Backfill skipped"
    "$BIN" sweep --skip-existing | sed 's/^/  /'
fi

if [ "$INTEGRATE" = "1" ]; then
    step "Integration with Claude Code and Codex"
    "$BIN" integrate install --bin "$BIN" | sed 's/^/  /'
fi

if [ "$TIMER" = "1" ]; then
    step "Synchronisation timer (every $INTERVAL)"
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
        mkdir -p "$UNITS"
        render "$REPO/host/systemd/memex-sync.service" "$UNITS/memex-sync.service" "MEMEX_BIN=$BIN"
        render "$REPO/host/systemd/memex-sync.timer" "$UNITS/memex-sync.timer" "INTERVAL=$INTERVAL"
        systemctl --user daemon-reload
        systemctl --user enable --now memex-sync.timer >/dev/null
        say "  memex-sync.timer enabled"
    else
        warn "systemd (user session) is not available. Add this line to your crontab (crontab -e):"
        say "    */10 * * * * $BIN sync --quiet"
    fi
fi

step "Status"
"$BIN" doctor || true

if [ -n "$PUBKEY" ]; then
    DEVICE_NAME="$("$BIN" status | sed -n 's/^device *: *//p')"
    cat <<EOF

Last step: authorise this machine on the server. From an administrator
account on the server:

  sudo memex server device add $DEVICE_NAME "$PUBKEY"

Then, here:  memex doctor && memex sync
EOF
fi
