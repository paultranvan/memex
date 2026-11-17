"""The server pipeline: the archivist (extract, then integrate) and the weaver.

Principle: the model writes, the code keeps the books. All the bookkeeping
(index, journal, ledger, sources, lock, commits) is deterministic; the agent
only writes pages, in a disposable working copy, and its work enters the wiki
only once it has been validated.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import frontmatter, i18n, index, segment as segment_mod
from .agent import Agent, AgentError, parse_json_reply
from .config import Config
from .gate import archive_path
from .lint import lint_wiki, summary as lint_summary
from .notify import notify
from .pages import Page, load_wiki, parse_page, wikilink
from .segment import Segment
from .util import (
    LockBusy, append_line, atomic_write, eprint, file_lock, iso, local_tz, now, read_json, safe_filename,
    sha256_text, slugify, write_json,
)
from .validate import Verdict, validate
from .vault import Vault
from .wikirepo import LandConflict, WikiRepo

ITEM_KINDS = {"decision", "dead_end", "problem", "fact", "preference", "state", "synthesis"}
PACKAGE = Path(__file__).resolve().parent
RESULT_FILE = ".memex-result.json"
# A page's header keeps only its latest sources: the full provenance is in the ledger and in git.
SOURCES_KEPT = 5
# Concept candidates offered to the weaver on each run, the most widely shared first.
WEAVER_CONCEPTS = 5


@dataclass
class Outcome:
    segment: str
    status: str            # integrated | nothing | short | failed | quarantined | unavailable
    title: str = ""
    folder: str = ""
    pages: list[str] = field(default_factory=list)
    detail: str = ""
    agent: bool = False    # the failure comes from a call to the agent, not from a rejection by the guardrails


class Server:
    def __init__(self, config: Config):
        self.config = config
        self.repo = WikiRepo(config)
        self.locale = i18n.use_for(self.repo.dir, config.get("MEMEX_LANG"))
        self.vault = Vault(config, self.repo.dir)
        self.agent = Agent(config)
        self.tz = local_tz(config.get("MEMEX_TZ"))
        self.lock_path = config.state_dir / "pipeline.lock"
        self.failures_path = config.state_dir / "failures.json"
        self.down_path = config.state_dir / "agent-down.json"
        self._down = False

    # ── setup ─────────────────────────────────────────────────────────────
    def init(self) -> bool:
        config = self.config
        for folder in (config.inbox, config.archive, config.extracts, config.quarantine, config.work,
                       config.state_dir, config.logs, config.backups):
            folder.mkdir(parents=True, exist_ok=True)
        created = self.repo.init(i18n.use_for(self.repo.dir, config.get("MEMEX_LANG")).lang)
        self.locale = i18n.use_for(self.repo.dir, config.get("MEMEX_LANG"))
        if created:
            index.write(self.repo.dir, load_wiki(self.repo.dir))
            self.repo.commit_all(self.repo.dir, "initial index", "memex")
            self.vault.export()
        return created

    def prompt(self, name: str, **values: str) -> str:
        lang = i18n.current().lang
        custom = self.config.get("MEMEX_PROMPTS_DIR").strip()
        folder = Path(custom).expanduser() if custom else None
        candidates = [folder / lang / f"{name}.md", folder / f"{name}.md"] if folder else []
        path = next((p for p in candidates if p.is_file()), PACKAGE / "prompts" / lang / f"{name}.md")
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    def conventions(self, root: Path) -> str:
        try:
            return frontmatter.split((root / "AGENTS.md").read_text(encoding="utf-8"))[1].strip()
        except OSError:
            return i18n.current().t("glue.no_conventions")

    # ── syncing with the vault ────────────────────────────────────────────
    def sync_in(self) -> list[str]:
        """Brings back the human edits made in Obsidian and commits them."""
        if not self.repo.is_clean():
            self.repo.reset_main_checkout()
        changed = self.vault.import_changes()
        if changed:
            index.write(self.repo.dir, load_wiki(self.repo.dir))
            shown = ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
            self.repo.commit_all(self.repo.dir, f"human: edits from Obsidian ({shown})", "human")
        return changed

    def sync_out(self) -> None:
        self.vault.export()

    # ── ledger, journal, operations log ───────────────────────────────────
    def ledger_ids(self, root: Optional[Path] = None) -> set[str]:
        ids = set()
        try:
            with open((root or self.repo.dir) / ".memex" / "ledger.jsonl", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        ids.add(json.loads(line)["id"])
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass
        return ids

    def _ledger(self, root: Path, seg: Segment, outcome: Outcome) -> None:
        append_line(root / ".memex" / "ledger.jsonl", json.dumps({
            "id": seg.id, "file": seg.path.name if seg.path else "", "device": seg.device, "source": seg.source,
            "started": iso(seg.started), "processed": iso(), "status": outcome.status, "folder": outcome.folder,
            "pages": outcome.pages,
        }, ensure_ascii=False))

    def _journal(self, root: Path, seg: Segment, outcome: Outcome, pages: dict[str, Page]) -> None:
        loc = i18n.current()
        local = seg.started.astimezone(self.tz)
        day = local.strftime("%Y-%m-%d")
        path = root / loc.journal / local.strftime("%Y") / f"{day}.md"
        if not path.exists():
            title = loc.t("journal.title", day=day)
            atomic_write(path, frontmatter.dump({"type": loc.type("journal"), loc.k_title: title}, f"# {title}\n\n"))
        where = seg.project_name or outcome.folder.split("/")[-1] or loc.t("journal.no_project")
        if outcome.folder and f"{outcome.folder}/{outcome.folder.split('/')[-1]}.md" in pages:
            where = wikilink(f"{outcome.folder}/{outcome.folder.split('/')[-1]}.md", outcome.folder.split("/")[-1])
        text = " ".join((outcome.detail or seg.title or loc.t("journal.untitled")).split())
        line = loc.t(
            "journal.line", time=local.strftime("%H:%M"), where=where, source=seg.source, device=seg.device, text=text,
        )
        if outcome.pages:
            links = ", ".join(wikilink(p, pages[p].title if p in pages else "") for p in outcome.pages[:6])
            line += f" → {links}"
        append_line(path, line + f" `{seg.id}`")

    def _log(self, root: Path, role: str, title: str, lines: list[str]) -> None:
        stamp = now().astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        entry = f"\n## [{stamp}] {role} | {' '.join(title.split())}\n" + "".join(f"- {line}\n" for line in lines)
        with open(root / "log.md", "a", encoding="utf-8") as handle:
            handle.write(entry)

    def _stamp_pages(self, root: Path, paths: list[str], source_id: str = "") -> None:
        """Provenance kept by the code: every page touched gets the source and the date."""
        loc = i18n.current()
        today = now().astimezone(self.tz).strftime("%Y-%m-%d")
        for relative in paths:
            target = root / relative
            try:
                page = parse_page(relative, target.read_text(encoding="utf-8"))
            except OSError:
                continue
            updates: dict[str, Any] = {}
            if source_id:
                sources = [s for s in frontmatter.as_list(page.meta.get(i18n.K_SOURCES)) if s != source_id]
                updates[i18n.K_SOURCES] = (sources + [source_id])[-SOURCES_KEPT:]
            updates[loc.k_updated] = today
            text = target.read_text(encoding="utf-8")
            stamped = frontmatter.set_keys(text, updates)
            if stamped != text:
                atomic_write(target, stamped)

    # ── tying a session to a folder ───────────────────────────────────────
    def _projects(self, root: Path) -> dict:
        data = read_json(root / ".memex" / "projects.json", {}) or {}
        return data if isinstance(data, dict) else {}

    def folder_for(self, root: Path, seg: Segment) -> tuple[str, bool]:
        """(folder, new). An empty string if the session is not tied to any project."""
        if not seg.project:
            return "", False
        mapping = self._projects(root)
        for key in (seg.project_remote, seg.project):
            if key and key in mapping and (root / mapping[key]).is_dir():
                return mapping[key], False
        wanted = seg.project_name or seg.project
        name = safe_filename(
            wanted if any(c.isupper() for c in wanted) else wanted.replace("-", " ").replace("_", " ").capitalize()
        )
        # A folder with the same name may already exist: created by hand, moved by the curator,
        # or opened as a topic before the project had a repository.
        loc = i18n.current()
        for top in (loc.projects, loc.topics):
            if (root / top).is_dir():
                for existing in sorted((root / top).iterdir()):
                    if existing.is_dir() and existing.name.lower() == name.lower():
                        return f"{top}/{existing.name}", False
        return f"{loc.projects}/{name}", True

    def _remember_project(self, root: Path, seg: Segment, folder: str) -> None:
        loc = i18n.current()
        if not seg.project or folder.split("/")[0] not in (loc.projects, loc.topics) or not (root / folder).is_dir():
            return
        mapping = self._projects(root)
        for key in (seg.project_remote, seg.project):
            if key:
                mapping[key] = folder
        write_json(root / ".memex" / "projects.json", mapping)

    # ── extraction ────────────────────────────────────────────────────────
    def meta_block(self, seg: Segment) -> str:
        loc = i18n.current()
        local = seg.started.astimezone(self.tz)
        rows = [
            loc.t("meta.date", value=local.strftime("%Y-%m-%d %H:%M")),
            loc.t("meta.source", source=seg.source, device=seg.device),
            loc.t("meta.project", name=seg.project_name, remote=seg.project_remote or loc.t("meta.no_remote"))
            if seg.project else loc.t("meta.no_project"),
            loc.t("meta.cwd", value=seg.cwd) if seg.cwd else "",
            loc.t("meta.branch", value=seg.branch) if seg.branch else "",
            loc.t("meta.title", value=seg.title) if seg.title else "",
            loc.t("meta.files", value=", ".join(seg.files[:15])) if seg.files else "",
        ]
        return "\n".join(row for row in rows if row)

    def extract(self, seg: Segment) -> dict:
        cache = self.config.extracts / f"{seg.id}.json"
        cached = read_json(cache, None)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list):
            return cached
        prompt = self.prompt("extract", meta=self.meta_block(seg), transcript=seg.transcript())
        reply = parse_json_reply(self.agent.run("extract", prompt, self.config.work, f"{seg.id}", role="extract").text)
        items = []
        for raw in reply.get("items") if isinstance(reply.get("items"), list) else []:
            if not isinstance(raw, dict):
                continue
            kind, title = str(raw.get("kind", "")).strip(), str(raw.get("title", "")).strip()
            content = str(raw.get("content", "")).strip()
            if kind not in ITEM_KINDS or not title or not content:
                continue
            items.append({
                "kind": kind, "title": title[:160], "content": content[:3000],
                "reason": str(raw.get("reason", "")).strip()[:1500],
                "entities": [str(e)[:60] for e in raw.get("entities", []) if isinstance(e, (str, int))][:6]
                if isinstance(raw.get("entities"), list) else [],
                "explicit": bool(raw.get("explicit")),
            })
        result = {
            "summary": str(reply.get("summary", "")).strip()[:400],
            "topic": str(reply.get("topic", "")).strip()[:80],
            "items": items[:12],
        }
        write_json(cache, result)
        return result

    # ── integration ───────────────────────────────────────────────────────
    def _where(self, folder: str, new: bool, extracted: dict) -> str:
        loc = i18n.current()
        if folder and new:
            return loc.t("where.new", folder=folder, name=folder.split("/")[-1])
        if folder:
            return loc.t("where.known", folder=folder, name=folder.split("/")[-1])
        hint = loc.t("where.hint", topic=extracted.get("topic")) if extracted.get("topic") else ""
        return loc.t("where.none", hint=hint, topics=loc.topics)

    def integrate(self, seg: Segment, extracted: dict) -> tuple[Outcome, Optional[Verdict]]:
        # Fixed name: runs are serial, and some harnesses create one state folder per directory.
        name = "ingest"
        feedback = ""
        max_chars = self.config.int("MEMEX_MAX_PAGE_CHARS")
        living_chars = self.config.int("MEMEX_LIVING_PAGE_CHARS")
        for attempt in (1, 2):
            path = self.repo.new_worktree(name)
            base = self.repo.head("HEAD", cwd=path)
            folder, new = self.folder_for(path, seg)
            prompt = self.prompt(
                "integrate",
                conventions=self.conventions(path), meta=self.meta_block(seg),
                folder=self._where(folder, new, extracted),
                items=json.dumps(extracted["items"], ensure_ascii=False, indent=2),
                # Flagged a little before the limit: the page is then tightened instead of extended.
                index=index.compact(load_wiki(path), long_page=int(max_chars * 0.8), long_living=int(living_chars * 0.8)),
                max_chars=str(max_chars), living_chars=str(living_chars),
            )
            if feedback:
                prompt += i18n.current().t("retry.archivist", feedback=feedback)
            try:
                self.agent.run("edit", prompt, path, f"{seg.id}-t{attempt}", role="integrate")
            except AgentError as exc:
                self.repo.drop_worktree(name)
                return Outcome(seg.id, "failed", seg.title, detail=str(exc)[:300], agent=True), None
            result = read_json(path / RESULT_FILE, {}) or {}
            (path / RESULT_FILE).unlink(missing_ok=True)
            today = seg.started.astimezone(self.tz).strftime("%Y-%m-%d")
            verdict = validate(self.repo, path, base, "archivist", today, self.config.int("MEMEX_MAX_PAGE_CHARS"))
            if verdict.ok:
                break
            feedback = verdict.feedback()
            self.repo.drop_worktree(name)
            if attempt == 2:
                detail = "rejected by the guardrails: " + "; ".join(verdict.errors)[:300]
                return Outcome(seg.id, "failed", seg.title, detail=detail), verdict
        summary = extracted.get("summary") or seg.title
        if not verdict.changed:
            self.repo.drop_worktree(name)
            if not result:
                # Neither a page nor a report: the agent did no work. Filing the session as
                # "nothing to record" would lose it.
                detail = "the agent produced nothing: neither a page nor a report"
                return Outcome(seg.id, "failed", seg.title, detail=detail, agent=True), verdict
            return Outcome(seg.id, "nothing", seg.title, detail=summary), verdict
        chosen = folder or self._folder_of_result(result, verdict.touched)
        outcome = Outcome(seg.id, "integrated", seg.title, chosen, sorted(verdict.touched), summary)
        self._stamp_pages(path, verdict.touched, seg.id)
        self._remember_project(path, seg, chosen)
        self._finish(path, seg, outcome, role="archivist")
        try:
            self.repo.commit_all(path, f"archivist: {seg.title or seg.id}\n\nsource: {seg.id}", "archivist")
            self.repo.land(name)
        except LandConflict as exc:
            self.repo.drop_worktree(name)
            return Outcome(seg.id, "failed", seg.title, detail=f"merge conflict: {exc}"[:300]), verdict
        self.repo.drop_worktree(name)
        return outcome, verdict

    @staticmethod
    def _folder_of_result(result: dict, touched: list[str]) -> str:
        loc = i18n.current()
        claimed = str(result.get("folder") or "").strip().strip("/")
        if claimed.split("/")[0] in (loc.projects, loc.topics) and claimed.count("/") == 1:
            return claimed
        for path in touched:
            parts = path.split("/")
            if parts[0] in (loc.projects, loc.topics) and len(parts) >= 3:
                return "/".join(parts[:2])
        return ""

    def _finish(self, root: Path, seg: Segment, outcome: Outcome, role: str) -> None:
        """Bookkeeping for a processed segment: journal, ledger, index, operations log."""
        pages = load_wiki(root)
        self._journal(root, seg, outcome, pages)
        self._ledger(root, seg, outcome)
        if outcome.pages:
            self._log(
                root, i18n.current().roles[role], seg.title or seg.id,
                [wikilink(p, pages[p].title if p in pages else "") for p in outcome.pages],
            )
        index.write(root, load_wiki(root))

    def _record_only(self, seg: Segment, outcome: Outcome) -> None:
        """A segment with nothing to record: one line in the journal, committed straight to the main repository."""
        self._finish(self.repo.dir, seg, outcome, role="archivist")
        self.repo.commit_all(self.repo.dir, f"journal: {seg.title or seg.id} ({outcome.status})", "memex")

    # ── one segment ───────────────────────────────────────────────────────
    def process(self, seg: Segment) -> Outcome:
        short = seg.user_chars < self.config.int("MEMEX_MIN_USER_CHARS") and seg.source != "note"
        if short:
            loc = i18n.current()
            detail = loc.t("journal.short", title=seg.title or loc.t("journal.no_title"))
            outcome = Outcome(seg.id, "short", seg.title, detail=detail)
            self._record_only(seg, outcome)
            return outcome
        try:
            extracted = self.extract(seg)
        except AgentError as exc:
            return Outcome(seg.id, "failed", seg.title, detail=f"extraction: {exc}"[:300], agent=True)
        if not extracted["items"]:
            outcome = Outcome(seg.id, "nothing", seg.title, detail=extracted.get("summary") or seg.title)
            self._record_only(seg, outcome)
            return outcome
        outcome, _verdict = self.integrate(seg, extracted)
        if outcome.status == "nothing":
            self._record_only(seg, outcome)
        return outcome

    def _archive(self, seg: Segment, quarantine: bool = False) -> None:
        if not seg.path:
            return
        target = (
            (self.config.quarantine / seg.device / seg.path.name) if quarantine
            else archive_path(self.config, seg.device, seg.path.name)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(seg.path, target)

    def pending(self) -> list[Path]:
        found = []
        if self.config.inbox.is_dir():
            for path in self.config.inbox.glob("*/*.md"):
                if not path.name.startswith("."):
                    found.append(path)
        return sorted(found, key=lambda p: p.name)

    # ── a full run ────────────────────────────────────────────────────────
    def run(self) -> int:
        """Processes everything that is waiting. Loops again as long as new segments arrive during the run."""
        code, started, failed = 0, time.time(), set()
        self._down = False
        try:
            with file_lock(self.lock_path):
                for _round in range(50):
                    code |= self._run(started, failed)
                    if self._down:
                        break
                    waiting = [p for p in self.pending() if p.name not in failed]
                    if not waiting or time.time() - started > self.config.int("MEMEX_RUN_BUDGET_S"):
                        break
        except LockBusy:
            eprint("memex: a run is already in progress.")
        return code

    def _run(self, started: float, failed: set) -> int:
        config = self.config
        self.init()
        self.repo.cleanup_stale()
        self._sweep_local_sources()
        human = self.sync_in()
        failures = read_json(self.failures_path, {}) or {}
        done = self.ledger_ids()
        outcomes: list[Outcome] = []
        budget, batch = config.int("MEMEX_RUN_BUDGET_S"), config.int("MEMEX_BATCH_MAX")
        left_over = 0
        for path in self.pending():
            if path.name in failed:
                continue
            if len(outcomes) >= batch or time.time() - started > budget:
                left_over += 1
                continue
            try:
                seg = segment_mod.load(path)
            except (ValueError, OSError) as exc:
                target = config.quarantine / path.parent.name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
                detail = f"unreadable segment: {exc}"[:200]
                outcomes.append(Outcome(path.stem, "quarantined", path.name, detail=detail))
                continue
            # The device is that of the inbox where the gate filed the file, never what the header says.
            seg.device = segment_mod.safe_token(path.parent.name, "unknown")
            if seg.id in done:
                self._archive(seg)
                continue
            try:
                outcome = self.process(seg)
            except Exception as exc:  # one segment must never block the ones after it
                self.repo.cleanup_stale()
                if not self.repo.is_clean():
                    self.repo.reset_main_checkout()
                outcome = Outcome(seg.id, "failed", seg.title, detail=f"internal error: {exc}"[:300])
            if outcome.status == "failed" and outcome.agent:
                healthy, why = self.agent.healthy(config.work)
                if not healthy:
                    # The agent no longer answers at all: this segment is not to blame. No failure is
                    # counted and the run stops, otherwise the whole queue would end up in quarantine.
                    outcome.status, outcome.detail = "unavailable", (why or outcome.detail)[:300]
                    outcomes.append(outcome)
                    self._down = True
                    break
            if outcome.status == "failed":
                failed.add(path.name)
                entry = failures.get(seg.id) or {"count": 0}
                entry = {"count": entry["count"] + 1, "error": outcome.detail, "date": iso()}
                failures[seg.id] = entry
                if entry["count"] >= config.int("MEMEX_MAX_RETRIES"):
                    self._archive(seg, quarantine=True)
                    failures.pop(seg.id, None)
                    outcome.status = "quarantined"
            else:
                failures.pop(seg.id, None)
                self._archive(seg)
                done.add(seg.id)
            write_json(self.failures_path, failures)
            outcomes.append(outcome)
            self.sync_out()
        weaver = ""
        newly_down = False
        if self._down:
            left_over = len(self.pending())
            newly_down = not self.down_path.exists()
            if newly_down:
                write_json(self.down_path, {"since": iso(), "error": outcomes[-1].detail})
        elif any(o.status in ("integrated", "nothing", "failed", "quarantined") for o in outcomes):
            self.down_path.unlink(missing_ok=True)
        if not self._down and (any(o.status == "integrated" for o in outcomes) or human):
            try:
                weaver = self.run_weaver()
            except Exception as exc:
                self.repo.cleanup_stale()
                weaver = f"internal error ({str(exc)[:120]})"
        self.sync_out()
        self._report(outcomes, weaver, human, left_over, newly_down)
        return 1 if any(o.status in ("failed", "quarantined", "unavailable") for o in outcomes) else 0

    def _sweep_local_sources(self) -> None:
        """Sessions held on the server itself (Hermes over a messaging app) come in through the same gate."""
        if not self.config.is_local_server or not self.config.list("MEMEX_SOURCES"):
            return
        from .ship import ship
        from .sweep import Sweeper

        try:
            Sweeper(self.config).run()
            ship(self.config)
        except Exception as exc:  # local capture must never block archiving
            eprint(f"memex: local sweep failed ({exc})")

    def _report(
        self, outcomes: list[Outcome], weaver: str, human: list[str], left_over: int, newly_down: bool = False,
    ) -> None:
        integrated = [o for o in outcomes if o.status == "integrated"]
        failed = [o for o in outcomes if o.status in ("failed", "quarantined")]
        quiet = [o for o in outcomes if o.status in ("nothing", "short")]
        lines = []
        for outcome in integrated:
            where = outcome.folder.split("/")[-1] if outcome.folder else "wiki"
            lines.append(f"• {where}: {outcome.detail} ({len(outcome.pages)} page(s))")
        if weaver:
            lines.append(f"• weaver: {weaver}")
        if human:
            lines.append(f"• {len(human)} edit(s) made in Obsidian picked up")
        if quiet:
            lines.append(f"• {len(quiet)} session(s) with nothing to record (journal only)")
        for outcome in failed:
            label = "quarantined" if outcome.status == "quarantined" else "failed, will retry on the next run"
            lines.append(f"⚠ {outcome.title or outcome.segment}: {label} — {outcome.detail}")
        for outcome in outcomes:
            if outcome.status == "unavailable":
                lines.append(
                    f'⚠ agent "{self.agent.name}" unavailable, run stopped, nothing lost — {outcome.detail}'
                )
        if left_over:
            lines.append(f"… {left_over} segment(s) left to process")
        if not lines:
            return
        for line in lines:
            eprint(line)
        level = self.config.get("MEMEX_NOTIFY_LEVEL").strip().lower()
        wanted = (
            level == "always"
            or (level == "changes" and (integrated or failed or weaver))
            or (level == "failures" and failed)
            or (level != "never" and newly_down)   # only once per outage, not on every run
        )
        if wanted:
            subject = f"memex: {len(integrated)} session(s) integrated" + (f", {len(failed)} failed" if failed else "")
            if newly_down:
                subject += ", agent unavailable"
            notify(self.config, subject, "\n".join(lines))

    # ── weaver ────────────────────────────────────────────────────────────
    def run_weaver(self, force: bool = False) -> str:
        config = self.config
        if not config.bool("MEMEX_WEAVER") and not force:
            return ""
        report = lint_wiki(config, self.repo.dir)
        if report.stats.get("pages", 0) < config.int("MEMEX_WEAVER_MIN_PAGES") and not force:
            return ""
        marker = config.state_dir / "weaver.json"
        state = read_json(marker, {}) or {}
        # A candidate that was already dismissed is offered again only if it gains a folder: otherwise
        # the same ones would come back on every run and take the place of the next ones.
        dismissed = state.get("dismissed") if isinstance(state.get("dismissed"), dict) else {}
        candidates = [
            c for c in report.concepts
            if len(c["folders"]) > int((dismissed.get(slugify(c["entity"])) or {}).get("folders", 0))
        ][:WEAVER_CONCEPTS]
        work = {
            "unlinked_mentions": report.unlinked[:40],
            "concept_candidates": candidates,
            "broken_links": report.broken[:20],
        }
        if not any(work.values()):
            return ""
        digest = sha256_text(json.dumps(work, sort_keys=True, ensure_ascii=False))
        if state.get("digest") == digest and not force:
            return ""  # same leads as on the last run: no point in asking again
        state = {"digest": digest, "date": iso(), "dismissed": dismissed}
        write_json(marker, state)
        name, feedback = "weaver", ""
        for attempt in (1, 2):
            path = self.repo.new_worktree(name)
            base = self.repo.head("HEAD", cwd=path)
            prompt = self.prompt(
                "weave", conventions=self.conventions(path),
                report=json.dumps(work, ensure_ascii=False, indent=2), index=index.compact(load_wiki(path)),
            )
            if feedback:
                prompt += i18n.current().t("retry.weaver", feedback=feedback)
            try:
                self.agent.run("edit", prompt, path, f"weaver-t{attempt}", role="weave")
            except AgentError as exc:
                self.repo.drop_worktree(name)
                return f"failed ({str(exc)[:120]})"
            (path / RESULT_FILE).unlink(missing_ok=True)
            today = now().astimezone(self.tz).strftime("%Y-%m-%d")
            verdict = validate(self.repo, path, base, "weaver", today, config.int("MEMEX_MAX_PAGE_CHARS"))
            if verdict.ok:
                break
            feedback = verdict.feedback()
            self.repo.drop_worktree(name)
            if attempt == 2:
                return "rejected by the guardrails"
        # The run went through: a candidate still without a page was dismissed knowingly.
        after = lint_wiki(config, path)
        still = {slugify(c["entity"]) for c in after.concepts}
        for candidate in candidates:
            key = slugify(candidate["entity"])
            if key in still:
                dismissed[key] = {"entity": candidate["entity"], "folders": len(candidate["folders"]), "date": iso()}
            else:
                dismissed.pop(key, None)
        write_json(marker, {**state, "dismissed": dismissed})
        if not verdict.changed:
            self.repo.drop_worktree(name)
            return ""
        loc = i18n.current()
        concepts = [p for p in verdict.touched if p.startswith(f"{loc.concepts}/")]
        self._stamp_pages(path, concepts)
        pages = load_wiki(path)
        self._log(
            path, loc.roles["weaver"], loc.t("log.linked", count=len(verdict.touched)),
            [wikilink(p, pages[p].title if p in pages else "") for p in sorted(verdict.touched)[:30]],
        )
        index.write(path, load_wiki(path))
        try:
            self.repo.commit_all(path, f"weaver: {len(verdict.touched)} page(s) linked", "weaver")
            self.repo.land(name)
        except LandConflict:
            self.repo.drop_worktree(name)
            return "merge conflict, postponed"
        self.repo.drop_worktree(name)
        created = [p for p in verdict.added if p.startswith(f"{loc.concepts}/")]
        return f"{len(verdict.touched)} page(s) linked" + (f", {len(created)} concept(s) created" if created else "")

    # ── status ────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        report = lint_wiki(self.config, self.repo.dir) if self.repo.exists() else None
        failures = read_json(self.failures_path, {}) or {}
        reviews = [r for r in (read_json(p, {}) for p in sorted((self.config.state_dir / "reviews").glob("*.json")))
                   if r and r.get("status") == "pending"] if (self.config.state_dir / "reviews").is_dir() else []
        return {
            "wiki": lint_summary(report) if report else "not initialised",
            "pending": len(self.pending()),
            "processed": len(self.ledger_ids()) if self.repo.exists() else 0,
            "failures": failures,
            "quarantined": len(list(self.config.quarantine.glob("*/*.md"))) if self.config.quarantine.is_dir() else 0,
            "pending_review": [r.get("id") for r in reviews],
            "agent": self.agent.name,
            "agent_unavailable": read_json(self.down_path, None) or "no",
            "sandbox": self.agent.sandbox_state(),
            "vault": str(self.config.vault_dir) if self.config.vault_dir else "(disabled)",
        }


def prune_extracts(config: Config, keep_days: int = 90) -> None:
    limit = time.time() - keep_days * 86400
    for path in config.extracts.glob("*.json") if config.extracts.is_dir() else []:
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            continue


__all__ = ["Server", "Outcome", "shutil"]
