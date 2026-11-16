#!/usr/bin/env bash
# Installe (ou met à jour) le serveur memex. À lancer sur le serveur, en root.
# Rejouable sans risque. Avec --user-mode, tout s'installe sans root, sous le
# compte courant : utile pour un essai, ou pour une machine tout-en-un.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib.sh
. "$REPO/scripts/lib.sh"

MEMEX_HOME_DIR="/srv/memex" MEMEX_USER="memex" AGENT="" VAULT="" USER_MODE=0 START=1
VERIF_CALENDAR="Sun *-*-* 05:00:00" DENY=()

usage() {
    cat <<'EOF'
Usage : server/install.sh [options]

  --home CHEMIN        racine de memex sur ce serveur (défaut : /srv/memex)
  --user NOM           compte système qui fait tourner memex (défaut : memex)
  --agent NOM          harnais des agents : hermes, claude ou codex (défaut : hermes)
  --vault CHEMIN       dossier du vault Obsidian synchronisé par LiveSync
  --deny CHEMIN        dossier rendu invisible aux agents (répétable), par exemple /var/www
  --verif CALENDRIER   quand passe le vérificateur (défaut : « Sun *-*-* 05:00:00 »)
  --user-mode          installation sans root, sous le compte courant
  --no-start           installer sans activer les minuteries
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --home) MEMEX_HOME_DIR="$2"; shift 2 ;;
        --user) MEMEX_USER="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --vault) VAULT="$2"; shift 2 ;;
        --deny) DENY+=("-$2"); shift 2 ;;
        --verif) VERIF_CALENDAR="$2"; shift 2 ;;
        --user-mode) USER_MODE=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "option inconnue : $1" ;;
    esac
done

step "Vérifications"
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
    [ "$(id -u)" = "0" ] || die "lance ce script en root (sudo), ou ajoute --user-mode."
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
    step "Compte système « $MEMEX_USER » (dossier : $MEMEX_HOME_DIR)"
    if id "$MEMEX_USER" >/dev/null 2>&1; then
        say "  déjà présent"
    else
        useradd --system --create-home --home-dir "$MEMEX_HOME_DIR" --shell /bin/bash "$MEMEX_USER"
        say "  créé, sans mot de passe ni sudo"
    fi
    mkdir -p "$MEMEX_HOME_DIR"
    chown "$MEMEX_USER:$MEMEX_USER" "$MEMEX_HOME_DIR"
    chmod 750 "$MEMEX_HOME_DIR"
else
    mkdir -p "$MEMEX_HOME_DIR"
fi

step "Installation du programme dans $APP"
install_app "$REPO/src" "$APP"
write_wrapper "$BIN" "$APP"
write_wrapper "$BIN_DIR/memex-gate" "$APP" gate
# Le code du pipeline appartient à root : les agents ne peuvent pas le modifier.
[ "$USER_MODE" = "0" ] && chown -R root:root "$APP" && chmod -R go-w "$APP"

step "Configuration : $CONFIG"
mkdir -p "$(dirname "$CONFIG")"
if [ ! -f "$CONFIG" ]; then
    cp "$REPO/config/memex.env.example" "$CONFIG"
    set_kv "$CONFIG" MEMEX_AGENT "${AGENT:-hermes}"
    say "  créée à partir du modèle"
else
    [ -n "$AGENT" ] && set_kv "$CONFIG" MEMEX_AGENT "$AGENT"
    say "  déjà présente : seules les options passées en argument sont modifiées"
fi
set_kv "$CONFIG" MEMEX_HOME "$MEMEX_HOME_DIR"
[ -n "$VAULT" ] && set_kv "$CONFIG" MEMEX_VAULT_DIR "$VAULT"
if [ "$USER_MODE" = "0" ]; then
    # Le serveur capture aussi ses propres discussions Hermes, par la même porte que les hôtes.
    set_kv "$CONFIG" MEMEX_SERVER_SSH local
    set_kv "$CONFIG" MEMEX_DEVICE serveur
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

step "Arborescence et dépôt du wiki"
as_memex env MEMEX_CONFIG="$CONFIG" "$BIN" server init | sed 's/^/  /'

CURRENT_AGENT="$(sed -n 's/^MEMEX_AGENT=//p' "$CONFIG" | tail -1 | tr -d '"')"
if [ "$CURRENT_AGENT" = "hermes" ]; then
    step "Skill Hermes « memex »"
    if [ "$USER_MODE" = "1" ]; then SKILLS="$HOME/.hermes/skills"; else SKILLS="$MEMEX_HOME_DIR/.hermes/skills"; fi
    mkdir -p "$SKILLS"
    rm -rf "$SKILLS/memex"
    cp -R "$REPO/server/hermes/skills/memex" "$SKILLS/memex"
    [ "$USER_MODE" = "0" ] && chown -R "$MEMEX_USER:$MEMEX_USER" "$MEMEX_HOME_DIR/.hermes"
    say "  installé dans $SKILLS/memex"
fi

step "Unités systemd"
mkdir -p "$UNITS"
EXTRA_RW=""
case "$VAULT" in ""|"$MEMEX_HOME_DIR"/*) ;; *) EXTRA_RW="$VAULT" ;; esac
for unit in memex-run.service memex-run.path memex-run.timer memex-verif.service memex-verif.timer memex-backup.service memex-backup.timer; do
    render "$REPO/server/systemd/$unit" "$UNITS/$unit" \
        "MEMEX_HOME=$MEMEX_HOME_DIR" "MEMEX_USER=$MEMEX_USER" "MEMEX_BIN=$BIN" \
        "EXTRA_RW=$EXTRA_RW" "DENY=${DENY[*]:-}" "VERIF_CALENDAR=$VERIF_CALENDAR"
    if [ "$USER_MODE" = "1" ]; then
        # Une unité utilisateur tourne déjà sous le bon compte, et le cloisonnement système n'y est pas disponible.
        sed -i -E '/^(User|Group|ProtectSystem|ReadWritePaths|ProtectHome|PrivateTmp|PrivateDevices|ProtectKernel(Tunables|Modules)|ProtectControlGroups|RestrictSUIDSGID|LockPersonality|RestrictRealtime|InaccessiblePaths|Environment=HOME)=?/d' "$UNITS/$unit"
        sed -i -E "s#^Environment=PATH=.*#Environment=PATH=$BIN_DIR:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin#; s#^WantedBy=multi-user.target#WantedBy=default.target#" "$UNITS/$unit"
    fi
done
sysctl_ daemon-reload
if [ "$START" = "1" ]; then
    sysctl_ enable --now memex-run.path memex-run.timer memex-verif.timer memex-backup.timer >/dev/null
    say "  memex-run (à l'arrivée d'un condensé et toutes les 15 min), memex-verif (hebdomadaire), memex-backup (quotidienne)"
else
    say "  installées, non activées (--no-start)"
fi

say ""
say "Le serveur memex est en place dans $MEMEX_HOME_DIR."
if [ "$USER_MODE" = "1" ]; then
    say ""
    say "Installation sans root : l'agent utilise les connexions de ton compte."
    say "Relier cette machine à ce serveur :  host/install.sh --server local --server-home \"$MEMEX_HOME_DIR\""
    say "Faire tourner les minuteries sans session ouverte :  loginctl enable-linger \"$MEMEX_USER\""
    say "Vérifier :  memex server status"
    exit 0
fi
cat <<EOF

Il reste à faire, une seule fois :

  1. Connecter l'agent sous le compte $MEMEX_USER (voir docs/RUNBOOK.md, « Agent ») :
       sudo -iu $MEMEX_USER    puis    hermes setup   (ou claude / codex)
  2. Autoriser chaque machine de travail avec la clé que son installeur affiche :
       sudo memex server device add NOM "ssh-ed25519 AAAA…"
  3. Facultatif : notifications (MEMEX_NOTIFY_CMD), vault LiveSync (server/livesync/README.md).

Vérifier :  sudo -u $MEMEX_USER memex server status
EOF
