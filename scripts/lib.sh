# Fonctions communes aux scripts d'installation. À sourcer, pas à exécuter.
# shellcheck shell=bash

say()  { printf '%s\n' "$*"; }
step() { printf '\n→ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf 'erreur : %s\n' "$*" >&2; exit 1; }

need() {
    command -v "$1" >/dev/null 2>&1 || die "« $1 » est introuvable. Installe-le puis relance."
}

check_python() {
    need python3
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        || die "Python 3.9 ou plus récent est requis (trouvé : $(python3 --version 2>&1))."
}

# set_kv FICHIER CLE VALEUR — pose ou remplace une ligne CLE=VALEUR.
set_kv() {
    local file="$1" key="$2" value="$3" tmp
    case "$value" in *[[:space:]]*) value="\"$value\"" ;; esac
    tmp="$(mktemp)"
    if [ -f "$file" ] && grep -q -E "^(export[[:space:]]+)?${key}=" "$file"; then
        awk -v k="$key" -v v="$value" '
            $0 ~ "^(export[[:space:]]+)?" k "=" && !done { print k "=" v; done = 1; next }
            { print }' "$file" > "$tmp"
    else
        [ -f "$file" ] && cat "$file" > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

# install_app SOURCE CIBLE — copie le paquet Python (ou le lie, en mode développement).
install_app() {
    local source="$1" target="$2" link="${3:-0}"
    mkdir -p "$(dirname "$target")"
    rm -rf "$target"
    if [ "$link" = "1" ]; then
        ln -s "$source" "$target"
    else
        mkdir -p "$target"
        cp -R "$source/memex" "$target/memex"
        find "$target" -name '__pycache__' -type d -prune -exec rm -rf {} +
    fi
}

# write_wrapper CHEMIN DOSSIER_APP [MODULE_ARGS…] — crée la commande `memex` (ou `memex-gate`).
write_wrapper() {
    local path="$1" app="$2"; shift 2
    mkdir -p "$(dirname "$path")"
    cat > "$path" <<EOF
#!/bin/sh
# Généré par memex. Relance l'installeur pour le mettre à jour.
exec env PYTHONPATH="$app\${PYTHONPATH:+:\$PYTHONPATH}" python3 -m memex $* "\$@"
EOF
    chmod 755 "$path"
}

# render MODELE CIBLE CLE=VALEUR… — remplace les @CLE@ d'un modèle.
render() {
    local template="$1" target="$2"; shift 2
    local content pair key value
    content="$(cat "$template")"
    for pair in "$@"; do
        key="${pair%%=*}"; value="${pair#*=}"
        content="${content//@${key}@/${value}}"
    done
    printf '%s\n' "$content" > "$target"
}
