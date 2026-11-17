"""The curator: it proposes, you approve, and only then does the wiki change.

Each action is a commit on a review branch. Approving an action means carrying
its commit over to `main`; rejecting it records it in the list of refusals, so
that it is not proposed again.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import i18n, index
from .agent import AgentError, parse_json_reply
from .lint import lint
from .notify import notify
from .pages import is_decision_path, load_wiki
from .pipeline import RESULT_FILE, Server
from .util import CommandError, LockBusy, append_line, eprint, file_lock, iso, now, parse_ts, read_json, write_json
from .validate import validate
from .wikirepo import LandConflict

ACTION_KINDS = {"merge", "condense", "fix", "delete", "move"}
PENDING, DONE, REJECTED, EXPIRED = "pending", "applied", "rejected", "expired"


class Reviews:
    def __init__(self, server: Server):
        self.server = server
        self.config = server.config
        self.repo = server.repo
        self.dir = self.config.state_dir / "reviews"

    # ── storage ───────────────────────────────────────────────────────────
    def _path(self, review_id: str) -> Path:
        return self.dir / f"{review_id}.json"

    def all(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        return [r for r in (read_json(p, None) for p in sorted(self.dir.glob("*.json"))) if isinstance(r, dict)]

    def pending(self) -> Optional[dict]:
        for review in reversed(self.all()):
            if review.get("status") == PENDING:
                return review
        return None

    def get(self, review_id: str = "") -> Optional[dict]:
        if not review_id:
            return self.pending()
        return read_json(self._path(review_id), None)

    def refused(self, root: Path) -> list[dict]:
        items = []
        try:
            with open(root / ".memex" / "refusals.jsonl", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        items.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return items[-60:]

    # ── curator run ───────────────────────────────────────────────────────
    def run(self, force: bool = False) -> int:
        try:
            with file_lock(self.server.lock_path, wait=True):
                return self._run(force)
        except LockBusy:
            return 0

    def _run(self, force: bool) -> int:
        server, repo, config = self.server, self.repo, self.config
        server.init()
        self.expire()
        if self.pending():
            eprint("memex: a review is already waiting for your answer; no new run.")
            return 0
        server.sync_in()
        head = repo.head("main")
        marker = config.state_dir / "curator.json"
        last = (read_json(marker, {}) or {}).get("head", "")
        if last == head and not force:
            eprint("memex: the wiki has not changed since the curator's last run.")
            return 0
        loc = i18n.current()
        report = lint(repo.dir, config.int("MEMEX_MAX_PAGE_CHARS"))
        changed = ""
        if last:
            try:
                changed = repo.git("diff", "--name-only", f"{last}..{head}", "--", *loc.content_dirs)
            except CommandError:
                changed = ""
        changed = "\n".join(f"- {line}" for line in changed.splitlines()[:80]) or loc.t("glue.first_pass")
        refused = "\n".join(
            f"- {r.get('kind')} · {r.get('title')} · {', '.join(r.get('pages', []))}" for r in self.refused(repo.dir)
        ) or loc.t("glue.none")
        review_id = now().strftime("%Y%m%d-%H%M")
        name = f"review-{review_id}"
        path = repo.new_worktree(name)
        max_actions = config.int("MEMEX_CURATOR_MAX_ACTIONS")
        prompt = server.prompt(
            "curate-diagnose", conventions=server.conventions(path),
            report=json.dumps(
                {"problems": report.problems[:40], "orphans": report.orphans[:30], "stats": report.stats},
                ensure_ascii=False, indent=2,
            ),
            changed=changed, index=index.compact(load_wiki(path)), refused=refused, max_actions=str(max_actions),
        )
        try:
            reply = server.agent.run("read", prompt, path, f"curate-diag-{review_id}", role="diagnose")
            plan = parse_json_reply(reply.text)
        except AgentError as exc:
            repo.drop_worktree(name)
            eprint(f"memex: the curator's diagnosis failed ({exc})")
            return 1
        repo.git("reset", "--quiet", "--hard", "HEAD", cwd=path)
        repo.git("clean", "-fdq", cwd=path)
        actions = []
        for raw in plan.get("actions") if isinstance(plan.get("actions"), list) else []:
            if not isinstance(raw, dict) or str(raw.get("kind")) not in ACTION_KINDS or not raw.get("title"):
                continue
            pages = (
                [str(p) for p in raw.get("pages", []) if isinstance(p, str)]
                if isinstance(raw.get("pages"), list) else []
            )
            if any(is_decision_path(p) for p in pages):
                continue
            actions.append({
                "kind": str(raw["kind"]), "title": str(raw["title"])[:200], "pages": pages[:10],
                "reason": str(raw.get("reason", ""))[:600], "instruction": str(raw.get("instruction", ""))[:1500],
            })
        questions = [
            {"title": str(q.get("title", ""))[:200], "detail": str(q.get("detail", ""))[:800]}
            for q in (plan.get("questions") if isinstance(plan.get("questions"), list) else [])
            if isinstance(q, dict) and q.get("title")
        ][:5]
        today = now().astimezone(server.tz).strftime("%Y-%m-%d")
        applied = []
        for number, action in enumerate(actions[:max_actions], start=1):
            base = repo.head("HEAD", cwd=path)
            entry = {**action, "n": number, "commit": "", "status": "failed", "summary": ""}
            try:
                apply_prompt = server.prompt(
                    "curate-apply", conventions=server.conventions(path),
                    action=json.dumps(action, ensure_ascii=False, indent=2),
                )
                server.agent.run("edit", apply_prompt, path, f"curate-{review_id}-a{number}", role="apply")
                result = read_json(path / RESULT_FILE, {}) or {}
                (path / RESULT_FILE).unlink(missing_ok=True)
                verdict = validate(repo, path, base, "curator", today, config.int("MEMEX_MAX_PAGE_CHARS"))
                if verdict.ok and verdict.changed:
                    server._stamp_pages(path, verdict.touched)
                    commit = repo.commit_all(path, f"curator: {action['title']}", "curator")
                    entry.update({
                        "commit": commit or "", "status": PENDING, "summary": str(result.get("summary", ""))[:300],
                        "modified": sorted(verdict.touched), "deleted": sorted(verdict.deleted),
                    })
                else:
                    entry["summary"] = "; ".join(verdict.errors)[:300] or "no change needed"
            except AgentError as exc:
                entry["summary"] = str(exc)[:300]
            if entry["status"] != PENDING:
                repo.git("reset", "--quiet", "--hard", base, cwd=path)
                repo.git("clean", "-fdq", cwd=path)
            applied.append(entry)
        repo.drop_worktree(name, keep_branch=True)
        write_json(marker, {"head": head, "date": iso()})
        proposals = [a for a in applied if a["status"] == PENDING]
        if not proposals and not questions:
            repo.git("branch", "-D", f"memex/{name}", check=False)
            eprint("memex: the curator has nothing to propose.")
            return 0
        review = {
            "id": review_id, "created": iso(), "base": head, "branch": f"memex/{name}",
            "status": PENDING if proposals else DONE, "actions": applied, "questions": questions,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(self._path(review_id), review)
        notify(config, f"memex: the curator proposes {len(proposals)} action(s)", self.format(review))
        print(self.format(review))
        return 0

    def format(self, review: dict) -> str:
        lines = [f"Review {review['id']} — {review.get('status')}"]
        for action in review.get("actions", []):
            if action.get("status") == "failed":
                continue
            mark = {PENDING: "", DONE: " ✓", REJECTED: " ✗"}.get(action.get("status"), "")
            lines.append(f"\n{action['n']}. [{action['kind']}] {action['title']}{mark}")
            if action.get("reason"):
                lines.append(f"   Why: {action['reason']}")
            if action.get("deleted"):
                lines.append(f"   Pages deleted: {', '.join(action['deleted'])}")
        for question in review.get("questions", []):
            lines.append(f"\n? To decide: {question['title']}\n   {question.get('detail', '')}")
        if review.get("status") == PENDING:
            lines.append('\nAnswer "ok", "ok except 2" or "no".')
        return "\n".join(lines)

    # ── approval ──────────────────────────────────────────────────────────
    def decide(
        self, review_id: str = "", only: Optional[list[int]] = None, skip: Optional[list[int]] = None,
        reject_all: bool = False,
    ) -> str:
        with file_lock(self.server.lock_path, wait=True):
            return self._decide(review_id, only, skip, reject_all)

    def _decide(self, review_id: str, only: Optional[list[int]], skip: Optional[list[int]], reject_all: bool) -> str:
        server, repo = self.server, self.repo
        review = self.get(review_id)
        if not review or review.get("status") != PENDING:
            return "No pending review."
        candidates = [a for a in review["actions"] if a.get("status") == PENDING]
        chosen = [] if reject_all else [
            a for a in candidates if (only is None or a["n"] in only) and not (skip and a["n"] in skip)
        ]
        refused = [a for a in candidates if a not in chosen]
        server.sync_in()
        landed, conflicts = [], []
        name = f"apply-{review['id']}"
        path = repo.new_worktree(name)
        for action in chosen:
            try:
                repo.git(
                    "cherry-pick", "--allow-empty", "--keep-redundant-commits", action["commit"], cwd=path,
                    author="curator",
                )
                action["status"] = DONE
                landed.append(action)
            except CommandError:
                repo.git("cherry-pick", "--abort", cwd=path, check=False)
                repo.git("reset", "--quiet", "--hard", "HEAD", cwd=path)
                action["status"] = "conflict"
                conflicts.append(action)
        for action in refused:
            action["status"] = REJECTED
            append_line(path / ".memex" / "refusals.jsonl", json.dumps({
                "date": iso(), "kind": action["kind"], "title": action["title"], "pages": action.get("pages", []),
            }, ensure_ascii=False))
        if landed:
            loc = i18n.current()
            lines = []
            for action in landed:
                lines.append(loc.t("log.action", kind=loc.kinds[action["kind"]], title=action["title"]))
                lines.extend(loc.t("log.deleted", path=p) for p in action.get("deleted", []))
            server._log(path, loc.roles["curator"], loc.t("log.review", id=review["id"], count=len(landed)), lines)
        index.write(path, load_wiki(path))
        try:
            message = f"curator: review {review['id']} ({len(landed)} applied, {len(refused)} rejected)"
            repo.commit_all(path, message, "curator")
            repo.land(name)
        except LandConflict as exc:
            repo.drop_worktree(name)
            return f"Merge failed: {exc}"
        repo.drop_worktree(name)
        server.sync_out()
        review["status"] = DONE if landed else REJECTED
        review["decided"] = iso()
        write_json(self._path(review["id"]), review)
        repo.git("branch", "-D", review.get("branch", ""), check=False)
        parts = [f"{len(landed)} action(s) applied", f"{len(refused)} rejected"]
        if conflicts:
            parts.append(f"{len(conflicts)} postponed: the page changed in the meantime")
        return "Review " + review["id"] + ": " + ", ".join(parts) + "."

    def expire(self) -> None:
        limit = time.time() - self.config.int("MEMEX_REVIEW_TTL_DAYS") * 86400
        for review in self.all():
            if review.get("status") != PENDING:
                continue
            created = parse_ts(review.get("created"))
            if created and created.timestamp() < limit:
                review["status"] = EXPIRED
                write_json(self._path(review["id"]), review)
                self.repo.git("branch", "-D", review.get("branch", ""), check=False)


def restore(server: Server, relative: str, rev: str = "") -> str:
    """Brings back a deleted page (or a given version of it) from the history."""
    repo = server.repo
    with file_lock(server.lock_path, wait=True):
        server.sync_in()
        if not rev:
            deleted = repo.last_commit_touching(relative, deleted=True)
            if deleted:
                rev = f"{deleted}^"
            else:
                return f'"{relative}" was not deleted: give a version with --rev.'
        content = repo.show(rev, relative)
        if content is None:
            return f'"{relative}" not found in version {rev}.'
        name = "restore"
        path = repo.new_worktree(name)
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        loc = i18n.current()
        server._log(path, loc.roles["restore"], relative, [loc.t("log.restored", rev=rev)])
        index.write(path, load_wiki(path))
        repo.commit_all(path, f"restore: {relative}", "memex")
        try:
            repo.land(name)
        finally:
            repo.drop_worktree(name)
        server.sync_out()
        return f'"{relative}" restored.'
