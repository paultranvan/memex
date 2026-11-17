#!/usr/bin/env bash
# Installs (or updates) the memex server. Run it on the server, as root.
# Safe to run again. With --user-mode, everything is installed without root,
# under the current account: useful for a trial, or for an all-in-one machine.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

MEMEX_HOME_DIR="/srv/memex" MEMEX_USER="memex" AGENT="" VAULT="" WIKI_LANG="" USER_MODE=0 START=1
CURATE_CALENDAR="Sun *-*-* 05:00:00" DENY=()

usage() {
    cat <<'EOF'
Usage: server/install.sh [options]

  --home PATH          root of memex on this server (default: /srv/memex)
  --user NAME          system account that runs memex (default: memex)
  --agent NAME         agent harness: hermes, claude or codex (default: hermes)
  --vault PATH         directory of the Obsidian vault synchronised by LiveSync
  --deny PATH          directory hidden from the agents (repeatable), for example /var/www
  --curate CALENDAR    when the curator runs (default: "Sun *-*-* 05:00:00")
  --lang en|fr         language of a new wiki (default: en); an existing wiki keeps its own
  --user-mode          install without root, under the current account
  --no-start           install without enabling the timers
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --home) MEMEX_HOME_DIR="$2"; shift 2 ;;
        --user) MEMEX_USER="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --vault) VAULT="$2"; shift 2 ;;
        --deny) DENY+=("-$2"); shift 2 ;;
        --curate) CURATE_CALENDAR="$2"; shift 2 ;;
        --lang) WIKI_LANG="$2"; shift 2 ;;
        --user-mode) USER_MODE=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done
case "$WIKI_LANG" in
    ""|en|fr) ;;
    *) die "--lang must be en or fr (got: $WIKI_LANG)" ;;
esac

step "Checks"
check_python
need git
if [ "$USER_MODE" = "1" ]; then
    MEMEX_USER="$(id -un)"
    [ "$MEMEX_HOME_DIR" = "/srv/memex" ] && MEMEX_HOME_DIR="$HOME/.local/share/memex-server"
    APP="$MEMEX_HOME_DIR/app"
    BIN_DIR="$HOME/.local/bin"
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/memex/memex.env"
    UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    as_memex() { "$@"; }
    sysctl_() { systemctl --user "$@"; }
else
    [ "$(id -u)" = "0" ] || die "run this script as root (sudo), or add --user-mode"
    need systemctl
    APP="/opt/memex/app"
    BIN_DIR="/usr/local/bin"
    CONFIG="/etc/memex/memex.env"
    UNITS="/etc/systemd/system"
    as_memex() { runuser -u "$MEMEX_USER" -- "$@"; }
    sysctl_() { systemctl "$@"; }
fi
BIN="$BIN_DIR/memex"

if [ "$USER_MODE" = "0" ]; then
    step "System account \"$MEMEX_USER\" (directory: $MEMEX_HOME_DIR)"
    if id "$MEMEX_USER" >/dev/null 2>&1; then
        say "  already present"
    else
        useradd --system --create-home --home-dir "$MEMEX_HOME_DIR" --shell /bin/bash "$MEMEX_USER"
        say "  created, with no password and no sudo"
    fi
    mkdir -p "$MEMEX_HOME_DIR"
    chown "$MEMEX_USER:$MEMEX_USER" "$MEMEX_HOME_DIR"
    chmod 750 "$MEMEX_HOME_DIR"
else
    mkdir -p "$MEMEX_HOME_DIR"
fi

step "Installing the program in $APP"
install_app "$REPO/src" "$APP"
write_wrapper "$BIN" "$APP"
write_wrapper "$BIN_DIR/memex-gate" "$APP" gate
# The pipeline code belongs to root: the agents cannot modify it.
[ "$USER_MODE" = "0" ] && chown -R root:root "$APP" && chmod -R go-w "$APP"

step "Configuration: $CONFIG"
mkdir -p "$(dirname "$CONFIG")"
if [ ! -f "$CONFIG" ]; then
    cp "$REPO/config/memex.env.example" "$CONFIG"
    set_kv "$CONFIG" MEMEX_AGENT "${AGENT:-hermes}"
    set_kv "$CONFIG" MEMEX_LANG "${WIKI_LANG:-en}"
    say "  created from the template"
else
    [ -n "$AGENT" ] && set_kv "$CONFIG" MEMEX_AGENT "$AGENT"
    [ -n "$WIKI_LANG" ] && set_kv "$CONFIG" MEMEX_LANG "$WIKI_LANG"
    say "  already present: only the options given on the command line are changed"
fi
set_kv "$CONFIG" MEMEX_HOME "$MEMEX_HOME_DIR"
[ -n "$VAULT" ] && set_kv "$CONFIG" MEMEX_VAULT_DIR "$VAULT"
if [ "$USER_MODE" = "0" ]; then
    # The server also captures its own Hermes conversations, through the same gate as the hosts.
    set_kv "$CONFIG" MEMEX_SERVER_SSH local
    set_kv "$CONFIG" MEMEX_DEVICE server
    set_kv "$CONFIG" MEMEX_DATA_DIR "$MEMEX_HOME_DIR/hostdata"
    CURRENT_AGENT="$(sed -n 's/^MEMEX_AGENT=//p' "$CONFIG" | tail -1 | tr -d '"')"
    if [ "$CURRENT_AGENT" = "hermes" ]; then
        set_kv "$CONFIG" MEMEX_SOURCES hermes
        set_kv "$CONFIG" MEMEX_HERMES_DB "$MEMEX_HOME_DIR/.hermes/state.db"
    else
        set_kv "$CONFIG" MEMEX_SOURCES ""
    fi
    chown "root:$MEMEX_USER" "$CONFIG"
    chmod 640 "$CONFIG"
fi

if [ -n "$VAULT" ]; then
    mkdir -p "$VAULT"
    [ "$USER_MODE" = "0" ] && chown "$MEMEX_USER:$MEMEX_USER" "$VAULT"
fi

step "Directory tree and wiki repository"
as_memex env MEMEX_CONFIG="$CONFIG" "$BIN" server init | sed 's/^/  /'

CURRENT_AGENT="$(sed -n 's/^MEMEX_AGENT=//p' "$CONFIG" | tail -1 | tr -d '"')"
if [ "$CURRENT_AGENT" = "hermes" ]; then
    step "Hermes skill \"memex\""
    if [ "$USER_MODE" = "1" ]; then SKILLS="$HOME/.hermes/skills"; else SKILLS="$MEMEX_HOME_DIR/.hermes/skills"; fi
    mkdir -p "$SKILLS"
    rm -rf "$SKILLS/memex"
    cp -R "$REPO/server/hermes/skills/memex" "$SKILLS/memex"
    [ "$USER_MODE" = "0" ] && chown -R "$MEMEX_USER:$MEMEX_USER" "$MEMEX_HOME_DIR/.hermes"
    say "  installed in $SKILLS/memex"
fi

step "systemd units"
mkdir -p "$UNITS"
UNIT_PATH="$BIN_DIR:$PATH"
if [ "$USER_MODE" = "1" ] && ! command -v "$CURRENT_AGENT" >/dev/null 2>&1; then
    warn "\"$CURRENT_AGENT\" was not found in the PATH: install it, then run this script again"
fi
EXTRA_RW=""
case "$VAULT" in ""|"$MEMEX_HOME_DIR"/*) ;; *) EXTRA_RW="$VAULT" ;; esac
for unit in memex-run.service memex-run.path memex-run.timer memex-curate.service memex-curate.timer memex-backup.service memex-backup.timer; do
    render "$REPO/server/systemd/$unit" "$UNITS/$unit" \
        "MEMEX_HOME=$MEMEX_HOME_DIR" "MEMEX_USER=$MEMEX_USER" "MEMEX_BIN=$BIN" \
        "EXTRA_RW=$EXTRA_RW" "DENY=${DENY[*]:-}" "CURATE_CALENDAR=$CURATE_CALENDAR"
    if [ "$USER_MODE" = "1" ]; then
        # A user unit already runs under the right account, and system-level confinement is not available there.
        sed -i -E '/^(User|Group|ProtectSystem|ReadWritePaths|ProtectHome|PrivateTmp|PrivateDevices|ProtectKernel(Tunables|Modules)|ProtectControlGroups|RestrictSUIDSGID|LockPersonality|RestrictRealtime|InaccessiblePaths|Environment=HOME)=?/d' "$UNITS/$unit"
        sed -i -E "s#^WantedBy=multi-user.target#WantedBy=default.target#" "$UNITS/$unit"
        # The unit gets the PATH of the installing shell: whatever finds the agent here will find it there
        # (version managers such as mise, nvm or asdf included).
        unit_tmp="$(mktemp)"
        while IFS= read -r line; do
            case "$line" in
                Environment=PATH=*) printf 'Environment="PATH=%s"\n' "${UNIT_PATH//%/%%}" ;;
                *) printf '%s\n' "$line" ;;
            esac
        done < "$UNITS/$unit" > "$unit_tmp"
        cat "$unit_tmp" > "$UNITS/$unit"
        rm -f "$unit_tmp"
    fi
done
sysctl_ daemon-reload
if [ "$START" = "1" ]; then
    sysctl_ enable --now memex-run.path memex-run.timer memex-curate.timer memex-backup.timer >/dev/null
    say "  memex-run (when a segment arrives and every 15 min), memex-curate (weekly), memex-backup (daily)"
else
    say "  installed, not enabled (--no-start)"
fi

say ""
say "The memex server is in place in $MEMEX_HOME_DIR."
if [ "$USER_MODE" = "1" ]; then
    say ""
    say "Installed without root: the agent uses the sign-ins of your account."
    say "Connect this machine to this server:  host/install.sh --server local --server-home \"$MEMEX_HOME_DIR\""
    say "Keep the timers running without an open session:  loginctl enable-linger \"$MEMEX_USER\""
    say "Check:  memex server status"
    exit 0
fi
cat <<EOF

Still to do, once:

  1. Sign the agent in under the $MEMEX_USER account (see docs/RUNBOOK.md, "Connect the agent"):
       sudo -iu $MEMEX_USER    then    hermes setup   (or claude / codex)
  2. Authorise each work machine with the key its installer prints:
       sudo memex server device add NAME "ssh-ed25519 AAAA…"
  3. Optional: notifications (MEMEX_NOTIFY_CMD), LiveSync vault (server/livesync/README.md).

Check:  sudo -u $MEMEX_USER memex server status
EOF
