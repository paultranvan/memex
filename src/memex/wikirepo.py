"""The wiki's git repository, server side.

`$MEMEX_HOME/wiki` is an ordinary repository whose `main` is the truth. Agents
never work in it: each run happens in a disposable working copy
(`git worktree`), validated and then merged as a fast-forward.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from . import i18n
from .config import Config
from .util import CommandError, run

AUTHORS = {
    "memex": ("memex", "memex@localhost"),
    "human": ("human", "human@localhost"),
    "archivist": ("archivist", "archivist@memex"),
    "weaver": ("weaver", "weaver@memex"),
    "curator": ("curator", "curator@memex"),
}
PACKAGE = Path(__file__).resolve().parent
GITIGNORE = ".obsidian/\n.trash/\n.livesync/\n.DS_Store\n.memex-result.json\n.memex-tmp/\n"


class LandConflict(RuntimeError):
    pass


class WikiRepo:
    def __init__(self, config: Config):
        self.config = config
        self.dir = config.wiki_repo
        self.work = config.work

    # ── low level ─────────────────────────────────────────────────────────
    def git(self, *args: str, cwd: Optional[Path] = None, check: bool = True, author: str = "memex") -> str:
        name, email = AUTHORS.get(author, AUTHORS["memex"])
        env = {
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": "memex", "GIT_COMMITTER_EMAIL": "memex@localhost",
            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8",
        }
        proc = run(["git", "-c", "core.quotepath=false", *args], cwd=cwd or self.dir, env=env, check=check)
        return proc.stdout.strip()

    def exists(self) -> bool:
        return (self.dir / ".git").exists()

    def head(self, ref: str = "main", cwd: Optional[Path] = None) -> str:
        return self.git("rev-parse", ref, cwd=cwd)

    # ── creation ──────────────────────────────────────────────────────────
    def init(self, lang: str) -> bool:
        if self.exists():
            return False
        loc = i18n.get(lang)
        conventions = (PACKAGE / "wiki_template" / loc.lang / "AGENTS.md").read_bytes()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "memex")
        self.git("config", "user.email", "memex@localhost")
        self.git("config", "gc.auto", "200")
        (self.dir / "AGENTS.md").write_bytes(conventions)
        (self.dir / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        (self.dir / "log.md").write_text(loc.t("log.header"), encoding="utf-8")
        (self.dir / ".memex").mkdir(exist_ok=True)
        (self.dir / ".memex" / "ledger.jsonl").write_text("", encoding="utf-8")
        (self.dir / ".memex" / "projects.json").write_text("{}\n", encoding="utf-8")
        (self.dir / ".memex" / "refusals.jsonl").write_text("", encoding="utf-8")
        i18n.record(self.dir, loc.lang)
        self.commit_all(self.dir, "wiki initialised", "memex")
        return True

    # ── commits ───────────────────────────────────────────────────────────
    def commit_all(self, cwd: Path, message: str, author: str) -> Optional[str]:
        self.git("add", "-A", cwd=cwd)
        if not self.git("status", "--porcelain", cwd=cwd):
            return None
        self.git("commit", "--quiet", "--no-verify", "-m", message, cwd=cwd, author=author)
        return self.head("HEAD", cwd=cwd)

    def stage_all(self, cwd: Path) -> None:
        self.git("add", "-A", cwd=cwd)

    def changes(self, cwd: Path, base: str) -> list[tuple[str, str]]:
        """(status, path) between `base` and the index of `cwd`. Renames are split into a deletion and an addition."""
        out = self.git("diff", "--cached", "--name-status", "--no-renames", "-z", base, cwd=cwd)
        parts = [part for part in out.split("\0") if part != ""]
        return [(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    def show(self, rev: str, path: str, cwd: Optional[Path] = None) -> Optional[str]:
        try:
            return run(["git", "-c", "core.quotepath=false", "show", f"{rev}:{path}"], cwd=cwd or self.dir).stdout
        except CommandError:
            return None

    def restore_path(self, cwd: Path, base: str, path: str) -> None:
        """Puts `path` back in the state it has in `base` (including when it did not exist there)."""
        if self.show(base, path, cwd=cwd) is None:
            self.git("rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", path, cwd=cwd)
            target = Path(cwd) / path
            if target.exists():
                target.unlink()
        else:
            self.git("checkout", "-q", base, "--", path, cwd=cwd)

    # ── working copies ────────────────────────────────────────────────────
    def worktree_path(self, name: str) -> Path:
        return self.work / name

    def new_worktree(self, name: str, base: str = "main") -> Path:
        self.drop_worktree(name)
        path = self.worktree_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--quiet", "-b", f"memex/{name}", str(path), base)
        return path

    def heal_worktree(self, path: Path) -> None:
        """Restores the `.git` file of a working copy if an agent replaced or deleted it."""
        path = Path(path)
        link = path / ".git"
        expected = f"gitdir: {self.dir / '.git' / 'worktrees' / path.name}\n"
        try:
            if link.is_file() and not link.is_symlink() and link.read_text(encoding="utf-8") == expected:
                return
        except OSError:
            pass
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link, ignore_errors=True)
        else:
            link.unlink(missing_ok=True)
        link.write_text(expected, encoding="utf-8")

    def drop_worktree(self, name: str, keep_branch: bool = False) -> None:
        path = self.worktree_path(name)
        self.git("worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        self.git("worktree", "prune", check=False)
        if not keep_branch:
            self.git("branch", "-D", f"memex/{name}", check=False)

    def land(self, name: str) -> str:
        """Merges the branch of a working copy into `main`, as a fast-forward."""
        path = self.worktree_path(name)
        if self.head("main") != self.git("merge-base", "main", "HEAD", cwd=path):
            try:
                self.git("merge", "--quiet", "--no-edit", "main", cwd=path)
            except CommandError as exc:
                self.git("merge", "--abort", cwd=path, check=False)
                raise LandConflict(str(exc)) from exc
        self.git("merge", "--quiet", "--ff-only", f"memex/{name}")
        return self.head("main")

    def is_clean(self) -> bool:
        return not self.git("status", "--porcelain")

    def reset_main_checkout(self) -> None:
        """The main repository must hold only what `main` holds."""
        self.git("reset", "--quiet", "--hard", "HEAD")
        self.git("clean", "-fdq")

    def cleanup_stale(self) -> None:
        self.git("worktree", "prune", check=False)
        if self.work.is_dir():
            for entry in self.work.iterdir():
                if entry.is_dir() and not entry.name.startswith("review-"):
                    self.drop_worktree(entry.name)

    def last_commit_touching(self, path: str, deleted: bool = False) -> Optional[str]:
        args = ["log", "-1", "--format=%H"]
        if deleted:
            args.append("--diff-filter=D")
        out = self.git(*args, "--", path, check=False)
        return out or None

    def set_env_for_agent(self) -> dict:
        return {"GIT_OPTIONAL_LOCKS": "0", "GIT_DIR": os.devnull}
