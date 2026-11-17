"""Command line: `memex …` on the host, `memex server …` on the server."""
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
    parser = argparse.ArgumentParser(prog="memex", description="A knowledge base fed by agent sessions.")
    parser.add_argument("--config", help="configuration file (default: ~/.config/memex/memex.env)")
    parser.add_argument("--version", action="version", version=f"memex {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("sync", help="sweep the sessions, send the segments, update the local wiki")
    p.add_argument("--session", action="append", default=[], help="sweep a session even if it is still active")
    p.add_argument("--all", action="store_true", help="also sweep the sessions that are still active")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("sweep", help="sweep only (send nothing)")
    p.add_argument("--session", action="append", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="write nothing, show what would be produced")
    p.add_argument(
        "--skip-existing", action="store_true",
        help="mark the existing sessions as already seen, without capturing them",
    )

    sub.add_parser("ship", help="send the pending segments")
    sub.add_parser("pull", help="update the local copy of the wiki")
    sub.add_parser("resend", help="queue every segment already sent again (to rebuild the server)")

    p = sub.add_parser("note", help="record a note right away")
    p.add_argument("text", nargs="*", help="the text (otherwise read from standard input)")
    p.add_argument("--title", default="")

    p = sub.add_parser("import", help="import a ChatGPT or claude.ai export (experimental)")
    p.add_argument("kind", choices=["chatgpt", "claude"])
    p.add_argument("path")
    p.add_argument("--since", default="", help="skip older conversations (YYYY-MM-DD)")
    p.add_argument("--min-chars", type=int, default=400, help="minimum length of what you wrote")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("hook", help="entry point for the Claude Code and Codex hooks")
    p.add_argument("event", choices=["session-start", "session-end"])

    p = sub.add_parser("integrate", help="wire memex into Claude Code and Codex, or remove it")
    p.add_argument("action", choices=["install", "remove"])
    p.add_argument("--bin", default="", help="path of the memex command to write into the hooks")
    p.add_argument("--only", choices=["claude", "codex"], default="")

    sub.add_parser("status", help="status of this machine")
    sub.add_parser("doctor", help="check the installation on this machine")

    server = sub.add_parser("server", help="server commands")
    ssub = server.add_subparsers(dest="server_command", metavar="command")
    ssub.add_parser("init", help="create the directory tree and the wiki repository")
    ssub.add_parser("run", help="one run: the archivist, then the weaver")
    p = ssub.add_parser("weave", help="run the weaver alone")
    p.add_argument("--force", action="store_true")
    p = ssub.add_parser("curate", help="run the curator (proposes a review)")
    p.add_argument("--force", action="store_true")
    p = ssub.add_parser("review", help="show, approve or reject the curator's review")
    p.add_argument("action", nargs="?", default="show", choices=["show", "approve", "reject", "list"])
    p.add_argument("--id", default="")
    p.add_argument("--only", default="", help="apply only these actions: 1,3")
    p.add_argument("--except", dest="skip", default="", help="apply all but these: 2")
    p = ssub.add_parser("restore", help="bring back a deleted page or an older version")
    p.add_argument("path")
    p.add_argument("--rev", default="")
    ssub.add_parser("backup", help="create a backup")
    p = ssub.add_parser("restore-backup", help="rebuild the server from a backup")
    p.add_argument("file")
    p.add_argument("--force", action="store_true")
    p = ssub.add_parser("device", help="manage the machines' SSH keys")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("key", nargs="*", default=[])
    ssub.add_parser("status", help="status of the server")
    ssub.add_parser("lint", help="deterministic check of the wiki")
    return parser


# ── host ──────────────────────────────────────────────────────────────────
def cmd_status(config: Config) -> int:
    from . import integrate

    outbox = len(list(config.outbox.glob("*.md"))) if config.outbox.is_dir() else 0
    sent = len(list(config.sent.rglob("*.md"))) if config.sent.is_dir() else 0
    last = read_json(config.data_dir / "last-sync.json", {}) or {}
    age = f"{int((time.time() - last['at']) / 60)} min ago" if last.get("at") else "never"
    print(f"device          : {config.device}")
    print(f"configuration   : {config.path or '(defaults)'}")
    print(f"server          : {config.server_ssh or '(offline: no server configured)'}")
    print(f"sources         : {', '.join(config.list('MEMEX_SOURCES'))}")
    print(f"segments        : {outbox} pending, {sent} sent")
    print(f"last sync       : {age}" + (f" — {last.get('ship')}" if last.get("ship") else ""))
    wiki = config.wiki_clone
    print(f"local wiki      : {wiki if (wiki / 'index.md').exists() else '(not cloned yet)'}")
    state = integrate.status(config)
    print("integrations    : " + ", ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in state.items()))
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
    check(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "Python 3.9 or newer is required")
    check(bool(shutil.which("git")), "git", "install git")
    check(config.path is not None, f"configuration: {config.path}", "no memex.env file found")
    sources = (
        ("claude-code", "MEMEX_CLAUDE_PROJECTS_DIR"), ("codex", "MEMEX_CODEX_SESSIONS_DIR"),
        ("hermes", "MEMEX_HERMES_DB"),
    )
    for name, key in sources:
        if name in config.list("MEMEX_SOURCES"):
            found = config.path_value(key).exists()
            label = f"source {name}: {config.path_value(key)}"
            print(("  ✓ " if found else "  – ") + label + ("" if found else " (no session yet)"))
    if not config.server_ssh:
        check(False, "server", "MEMEX_SERVER_SSH is empty: segments pile up and never leave")
    else:
        try:
            answer = json.loads(Transport(config).gate(["ping"]).stdout.decode("utf-8"))
            seen_as = answer.get("device") or "local"
            check(bool(answer.get("ok")), f'server reachable ({config.server_ssh}), seen as "{seen_as}"')
        except Exception as exc:
            check(False, f"server reachable ({config.server_ssh})", str(exc)[:160])
        check((config.wiki_clone / "index.md").exists(), f"local wiki: {config.wiki_clone}", "run `memex pull`")
    state = integrate.status(config)
    fix = "run `memex integrate install`"
    if integrate.claude_dir().is_dir():
        check(state["claude_hooks"] and state["claude_pointer"], "Claude Code wired in", fix)
    if integrate.codex_dir().is_dir():
        check(state["codex_hooks"] and state["codex_pointer"], "Codex wired in", fix)
    if shutil.which("systemctl"):
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "memex-sync.timer"], capture_output=True, text=True,
        ).stdout.strip()
        check(active == "active", "memex-sync timer", "run `systemctl --user enable --now memex-sync.timer`")
    print("Everything is in order." if not problems else f"{problems} item(s) to fix.")
    return 1 if problems else 0


# ── server ────────────────────────────────────────────────────────────────
def cmd_server(config: Config, args: argparse.Namespace) -> int:
    from . import serverops
    from .lint import lint, summary
    from .pipeline import Server
    from .review import Reviews, restore

    server = Server(config)
    command = args.server_command
    if command == "init":
        print("Wiki created." if server.init() else "Wiki already in place.")
        return 0
    if command == "run":
        return server.run()
    if command == "weave":
        from .util import file_lock

        with file_lock(server.lock_path, wait=True):
            server.sync_in()
            result = server.run_weaver(force=args.force)
            server.sync_out()
        print(result or "Nothing to link.")
        return 0
    if command == "curate":
        return Reviews(server).run(force=args.force)
    if command == "review":
        reviews = Reviews(server)
        if args.action == "list":
            for review in reviews.all():
                print(f"{review['id']}  {review.get('status')}  {len(review.get('actions', []))} action(s)")
            return 0
        if args.action == "show":
            review = reviews.get(args.id)
            print(reviews.format(review) if review else "No pending review.")
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
            print("\n".join(serverops.device_list(config)) or "(no device)")
        elif args.action == "remove":
            print(serverops.device_remove(config, args.name))
        else:
            print(serverops.device_add(config, args.name, " ".join(args.key)))
        return 0
    if command == "lint":
        report = lint(config.wiki_repo, config.int("MEMEX_MAX_PAGE_CHARS"), config.int("MEMEX_WEAVER_MIN_FOLDERS"))
        print(summary(report))
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
        return 0
    if command == "status":
        for key, value in server.status().items():
            print(f"{key:18s}: {value}")
        return 0
    print("Give a command: memex server --help", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    from .i18n import UnsupportedLanguage

    try:
        return _main(argv)
    except UnsupportedLanguage as exc:
        print(f"memex: {exc}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "gate":  # pass-through: the gate parses its own arguments
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

        report = Sweeper(config).run(
            force=args.session, force_all=args.all, dry_run=args.dry_run, discard=args.skip_existing,
        )
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

        print(f"{resend_all(config)} segment(s) queued again. Run `memex ship`.")
        return 0
    if command == "note":
        from .sweep import write_note

        text = " ".join(args.text).strip() or ("" if sys.stdin.isatty() else sys.stdin.read().strip())
        if not text:
            print("Nothing to record: give the text as an argument or on standard input.", file=sys.stderr)
            return 2
        write_note(config, text, title=args.title)
        from .hook import spawn_sync

        spawn_sync(config, throttle=False)
        print("Note recorded: it will be integrated into the wiki on the archivist's next run.")
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
