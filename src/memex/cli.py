"""Ligne de commande : `memex …` côté hôte, `memex server …` côté serveur."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config
from .util import parse_ts, read_json


def _numbers(value: str) -> list[int]:
    return [int(part) for part in value.replace(" ", "").split(",") if part.isdigit()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex", description="Base de connaissance alimentée par les sessions d'agents.")
    parser.add_argument("--config", help="fichier de configuration (défaut : ~/.config/memex/memex.env)")
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="commande")

    p = sub.add_parser("sync", help="balaye les sessions, envoie les condensés, met le wiki local à jour")
    p.add_argument("--session", action="append", default=[], help="force le balayage d'une session même active")
    p.add_argument("--all", action="store_true", help="balaye aussi les sessions encore actives")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("sweep", help="balaye seulement (sans rien envoyer)")
    p.add_argument("--session", action="append", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="n'écrit rien, montre ce qui serait produit")

    sub.add_parser("ship", help="envoie les condensés en attente")
    sub.add_parser("pull", help="met à jour la copie locale du wiki")
    sub.add_parser("resend", help="remet en file tous les condensés déjà envoyés (reconstruction du serveur)")

    p = sub.add_parser("note", help="consigne une note tout de suite")
    p.add_argument("text", nargs="*", help="le texte (sinon lu sur l'entrée standard)")
    p.add_argument("--title", default="")

    p = sub.add_parser("import", help="importe un export ChatGPT ou claude.ai (expérimental)")
    p.add_argument("kind", choices=["chatgpt", "claude"])
    p.add_argument("path")
    p.add_argument("--since", default="", help="ignore les conversations plus anciennes (AAAA-MM-JJ)")
    p.add_argument("--min-chars", type=int, default=400, help="longueur minimale de ce que tu as écrit")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("hook", help="point d'entrée des hooks de Claude Code et Codex")
    p.add_argument("event", choices=["session-start", "session-end"])

    p = sub.add_parser("integrate", help="branche ou débranche Claude Code et Codex")
    p.add_argument("action", choices=["install", "remove"])
    p.add_argument("--bin", default="", help="chemin de la commande memex à écrire dans les hooks")
    p.add_argument("--only", choices=["claude", "codex"], default="")

    sub.add_parser("status", help="état de cette machine")
    sub.add_parser("doctor", help="vérifie l'installation de cette machine")

    server = sub.add_parser("server", help="commandes du serveur")
    ssub = server.add_subparsers(dest="server_command", metavar="commande")
    ssub.add_parser("init", help="crée l'arborescence et le dépôt du wiki")
    ssub.add_parser("run", help="un passage : archiviste, puis tisseur")
    p = ssub.add_parser("tisseur", help="lance le tisseur seul")
    p.add_argument("--force", action="store_true")
    p = ssub.add_parser("verif", help="lance le vérificateur (propose une revue)")
    p.add_argument("--force", action="store_true")
    p = ssub.add_parser("review", help="voir, approuver ou refuser la revue du vérificateur")
    p.add_argument("action", nargs="?", default="show", choices=["show", "approve", "reject", "list"])
    p.add_argument("--id", default="")
    p.add_argument("--only", default="", help="n'appliquer que ces actions : 1,3")
    p.add_argument("--except", dest="skip", default="", help="tout appliquer sauf celles-ci : 2")
    p = ssub.add_parser("restore", help="rétablit une page supprimée ou une ancienne version")
    p.add_argument("path")
    p.add_argument("--rev", default="")
    ssub.add_parser("backup", help="crée une sauvegarde")
    p = ssub.add_parser("restore-backup", help="reconstruit le serveur depuis une sauvegarde")
    p.add_argument("file")
    p.add_argument("--force", action="store_true")
    p = ssub.add_parser("device", help="gère les clés SSH des machines")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("key", nargs="*", default=[])
    ssub.add_parser("status", help="état du serveur")
    ssub.add_parser("lint", help="contrôle déterministe du wiki")
    return parser


# ── hôte ──────────────────────────────────────────────────────────────────
def cmd_status(config: Config) -> int:
    from . import integrate

    outbox = len(list(config.outbox.glob("*.md"))) if config.outbox.is_dir() else 0
    sent = len(list(config.sent.rglob("*.md"))) if config.sent.is_dir() else 0
    last = read_json(config.data_dir / "last-sync.json", {}) or {}
    age = f"il y a {int((time.time() - last['at']) / 60)} min" if last.get("at") else "jamais"
    print(f"appareil        : {config.device}")
    print(f"configuration   : {config.path or '(valeurs par défaut)'}")
    print(f"serveur         : {config.server_ssh or '(hors-ligne : aucun serveur configuré)'}")
    print(f"sources         : {', '.join(config.list('MEMEX_SOURCES'))}")
    print(f"condensés       : {outbox} en attente, {sent} envoyé(s)")
    print(f"dernière synchro: {age}" + (f" — {last.get('ship')}" if last.get("ship") else ""))
    wiki = config.wiki_clone
    print(f"wiki local      : {wiki if (wiki / 'index.md').exists() else '(pas encore cloné)'}")
    state = integrate.status(config)
    print("branchements    : " + ", ".join(f"{name}={'oui' if ok else 'non'}" for name, ok in state.items()))
    return 0


def cmd_doctor(config: Config) -> int:
    from . import integrate
    from .ship import Transport

    problems = 0

    def check(ok: bool, label: str, hint: str = "") -> None:
        nonlocal problems
        if not ok:
            problems += 1
        print(("  ✓ " if ok else "  ✗ ") + label + (f" — {hint}" if hint and not ok else ""))

    print("memex doctor")
    check(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "Python 3.9 ou plus récent requis")
    check(bool(shutil.which("git")), "git", "installe git")
    check(config.path is not None, f"configuration : {config.path}", "aucun fichier memex.env trouvé")
    for name, key in (("claude-code", "MEMEX_CLAUDE_PROJECTS_DIR"), ("codex", "MEMEX_CODEX_SESSIONS_DIR"), ("hermes", "MEMEX_HERMES_DB")):
        if name in config.list("MEMEX_SOURCES"):
            check(config.path_value(key).exists(), f"source {name} : {config.path_value(key)}", "introuvable sur cette machine")
    if not config.server_ssh:
        check(False, "serveur", "MEMEX_SERVER_SSH est vide : les condensés s'accumulent sans partir")
    else:
        try:
            answer = json.loads(Transport(config).gate(["ping"]).stdout.decode("utf-8"))
            check(bool(answer.get("ok")), f"serveur joignable ({config.server_ssh}), vu comme « {answer.get('device') or 'local'} »")
        except Exception as exc:
            check(False, f"serveur joignable ({config.server_ssh})", str(exc)[:160])
        check((config.wiki_clone / "index.md").exists(), f"wiki local : {config.wiki_clone}", "lance `memex pull`")
    state = integrate.status(config)
    if integrate.claude_dir().is_dir():
        check(state["claude_hooks"] and state["claude_pointeur"], "Claude Code branché", "lance `memex integrate install`")
    if integrate.codex_dir().is_dir():
        check(state["codex_hooks"] and state["codex_pointeur"], "Codex branché", "lance `memex integrate install`")
    if shutil.which("systemctl"):
        active = subprocess.run(["systemctl", "--user", "is-active", "memex-sync.timer"], capture_output=True, text=True).stdout.strip()
        check(active == "active", "minuterie memex-sync", "lance `systemctl --user enable --now memex-sync.timer`")
    print("Tout est en ordre." if not problems else f"{problems} point(s) à corriger.")
    return 1 if problems else 0


# ── serveur ───────────────────────────────────────────────────────────────
def cmd_server(config: Config, args: argparse.Namespace) -> int:
    from . import serverops
    from .lint import lint, summary
    from .pipeline import Server
    from .review import Reviews, restore

    server = Server(config)
    command = args.server_command
    if command == "init":
        print("Wiki créé." if server.init() else "Wiki déjà en place.")
        return 0
    if command == "run":
        return server.run()
    if command == "tisseur":
        from .util import file_lock

        with file_lock(server.lock_path, wait=True):
            server.sync_in()
            result = server.run_tisseur(force=args.force)
            server.sync_out()
        print(result or "Rien à relier.")
        return 0
    if command == "verif":
        return Reviews(server).run(force=args.force)
    if command == "review":
        reviews = Reviews(server)
        if args.action == "list":
            for review in reviews.all():
                print(f"{review['id']}  {review.get('statut')}  {len(review.get('actions', []))} action(s)")
            return 0
        if args.action == "show":
            review = reviews.get(args.id)
            print(reviews.format(review) if review else "Aucune revue en attente.")
            return 0
        if args.action == "reject":
            print(reviews.decide(args.id, reject_all=True))
            return 0
        only = _numbers(args.only) if args.only else None
        print(reviews.decide(args.id, only=only, skip=_numbers(args.skip) if args.skip else None))
        return 0
    if command == "restore":
        print(restore(server, args.path, args.rev))
        return 0
    if command == "backup":
        print(serverops.backup(config))
        return 0
    if command == "restore-backup":
        print(serverops.restore_backup(config, Path(args.file), args.force))
        return 0
    if command == "device":
        if args.action == "list":
            print("\n".join(serverops.device_list()) or "(aucun appareil)")
        elif args.action == "remove":
            print(serverops.device_remove(args.name))
        else:
            print(serverops.device_add(config, args.name, " ".join(args.key)))
        return 0
    if command == "lint":
        report = lint(config.wiki_repo, config.int("MEMEX_MAX_PAGE_CHARS"), config.int("MEMEX_TISSEUR_MIN_DOSSIERS"))
        print(summary(report))
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
        return 0
    if command == "status":
        for key, value in server.status().items():
            print(f"{key:18s}: {value}")
        return 0
    print("Précise une commande : memex server --help", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "gate":  # passe-plat : la porte analyse elle-même ses arguments
        from . import gate

        return gate.main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    command = args.command
    if command == "hook":
        from . import hook

        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        if args.event == "session-start":
            text = hook.session_start(config, raw)
            if text:
                print(text)
        else:
            hook.session_end(config, raw)
        return 0
    if command == "sync":
        from .ship import sync

        return sync(config, tuple(args.session), args.all, args.quiet)
    if command == "sweep":
        from .sweep import Sweeper

        report = Sweeper(config).run(force=args.session, force_all=args.all, dry_run=args.dry_run)
        print(report.summary())
        if args.dry_run:
            print("\n".join(report.written))
        return 0
    if command == "ship":
        from .ship import ship

        report = ship(config)
        print(report.summary())
        return 1 if report.error else 0
    if command == "pull":
        from .ship import pull

        report = pull(config)
        print(report.summary())
        return 1 if report.error else 0
    if command == "resend":
        from .ship import resend_all

        print(f"{resend_all(config)} condensé(s) remis en file. Lance `memex ship`.")
        return 0
    if command == "note":
        from .sweep import write_note

        text = " ".join(args.text).strip() or ("" if sys.stdin.isatty() else sys.stdin.read().strip())
        if not text:
            print("Rien à consigner : donne le texte en argument ou sur l'entrée standard.", file=sys.stderr)
            return 2
        write_note(config, text, title=args.title)
        from .hook import spawn_sync

        spawn_sync(config, throttle=False)
        print("Note consignée : elle sera intégrée au wiki au prochain passage de l'archiviste.")
        return 0
    if command == "import":
        from . import imports

        since = parse_ts(args.since + "T00:00:00Z") if args.since else None
        report = imports.run(config, args.kind, Path(args.path).expanduser(), since, args.min_chars, args.dry_run)
        print(report.summary())
        return 0
    if command == "integrate":
        from . import integrate

        binary = args.bin or shutil.which("memex") or "memex"
        install = args.action == "install"
        lines = []
        if args.only in ("", "claude"):
            lines += integrate.claude(config, binary, install)
        if args.only in ("", "codex"):
            lines += integrate.codex(config, binary, install)
        print("\n".join(lines))
        return 0
    if command == "status":
        return cmd_status(config)
    if command == "doctor":
        return cmd_doctor(config)
    if command == "server":
        return cmd_server(config, args)
    parser.print_help()
    return 2
