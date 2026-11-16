#!/usr/bin/env bash
# Installe memex sur une machine de travail (Claude Code, Codex).
# Rejouable sans risque : chaque étape vérifie ce qui est déjà en place.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

SERVER="" SERVER_HOME="" DEVICE="" SOURCES="" EXCLUDE_PATHS="" EXCLUDE_REMOTES=""
TIMER=1 INTEGRATE=1 LINK=0 KEY=1 INTERVAL="10min" BACKFILL=1

usage() {
    cat <<'EOF'
Usage : host/install.sh [options]

  --server UTILISATEUR@HÔTE   adresse SSH du serveur memex (« local » : serveur sur cette machine)
  --server-home CHEMIN        valeur de MEMEX_HOME sur le serveur (défaut : /srv/memex)
  --device NOM                nom de cette machine (défaut : son nom d'hôte)
  --sources LISTE             sources à capturer (défaut : claude-code,codex)
  --exclude-paths LISTE       répertoires à ne jamais capturer
  --exclude-remotes LISTE     dépôts git à ne jamais capturer (motifs)
  --interval DURÉE            fréquence de la synchronisation (défaut : 10min)
  --no-backfill               ne pas reprendre l'historique : seules les sessions à venir sont capturées
  --no-timer                  ne pas installer la minuterie
  --no-integrate              ne pas toucher à la configuration de Claude Code et Codex
  --no-key                    ne pas créer de clé SSH dédiée
  --link                      lier le dépôt au lieu de le copier (développement)
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
        *) usage >&2; die "option inconnue : $1" ;;
    esac
done

step "Vérifications"
check_python
need git
need ssh

DATA="${MEMEX_DATA_DIR:-$HOME/.local/share/memex}"
APP="$DATA/app"
BIN="$HOME/.local/bin/memex"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/memex"
CONFIG="$CONFIG_DIR/memex.env"

step "Installation du programme dans $APP"
if [ "$LINK" = "1" ]; then install_app "$REPO/src" "$APP" 1; else install_app "$REPO/src" "$APP"; fi
write_wrapper "$BIN" "$APP"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "$HOME/.local/bin n'est pas dans ton PATH : ajoute-le pour taper « memex » directement." ;;
esac

step "Configuration : $CONFIG"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
    cp "$REPO/config/memex.env.example" "$CONFIG"
    chmod 600 "$CONFIG"
    say "  créée à partir du modèle"
else
    say "  déjà présente : seules les options passées en argument sont modifiées"
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
    step "Clé SSH dédiée à memex"
    KEYFILE="$HOME/.ssh/memex_ed25519"
    if [ ! -f "$KEYFILE" ]; then
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        ssh-keygen -q -t ed25519 -N "" -C "memex@$(hostname -s 2>/dev/null || hostname)" -f "$KEYFILE"
        say "  créée : $KEYFILE"
    else
        say "  déjà présente : $KEYFILE"
    fi
    set_kv "$CONFIG" MEMEX_SSH_OPTS "-i $KEYFILE -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=6"
    PUBKEY="$(cat "$KEYFILE.pub")"
fi

if [ "$BACKFILL" = "0" ] && [ ! -f "$DATA/state.json" ]; then
    step "Historique laissé de côté"
    "$BIN" sweep --skip-existing | sed 's/^/  /'
fi

if [ "$INTEGRATE" = "1" ]; then
    step "Branchement sur Claude Code et Codex"
    "$BIN" integrate install --bin "$BIN" | sed 's/^/  /'
fi

if [ "$TIMER" = "1" ]; then
    step "Minuterie de synchronisation (toutes les $INTERVAL)"
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
        mkdir -p "$UNITS"
        render "$REPO/host/systemd/memex-sync.service" "$UNITS/memex-sync.service" "MEMEX_BIN=$BIN"
        render "$REPO/host/systemd/memex-sync.timer" "$UNITS/memex-sync.timer" "INTERVAL=$INTERVAL"
        systemctl --user daemon-reload
        systemctl --user enable --now memex-sync.timer >/dev/null
        say "  memex-sync.timer activée"
    else
        warn "systemd (session utilisateur) indisponible. Ajoute cette ligne à ta crontab (crontab -e) :"
        say "    */10 * * * * $BIN sync --quiet"
    fi
fi

step "État"
"$BIN" doctor || true

if [ -n "$PUBKEY" ]; then
    DEVICE_NAME="$("$BIN" status | sed -n 's/^appareil *: //p')"
    cat <<EOF

Dernière étape : autorise cette machine sur le serveur. Depuis un compte
administrateur du serveur :

  sudo memex server device add $DEVICE_NAME "$PUBKEY"

Puis, ici :  memex doctor && memex sync
EOF
fi
