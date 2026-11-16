"""Le balayeur : transforme les sessions locales en condensés, dans la boîte d'envoi.

Sans LLM, incrémental (un curseur par session), idempotent. Une session n'est
balayée que si elle est inactive depuis MEMEX_IDLE_MINUTES, ou si on le force
(fin de session signalée par un hook).
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import adapters as adapters_mod
from .config import Config
from .project import ProjectResolver, exclusion_reason
from .redact import Redactor
from .segment import Message, Segment
from .util import LockBusy, UTC, atomic_write, file_lock, now, read_json, tilde, truncate_middle, write_json

STATE_VERSION = 1


@dataclass
class SweepReport:
    written: list[str] = field(default_factory=list)
    sessions: int = 0
    active: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    redactions: int = 0
    busy: bool = False

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1

    def summary(self) -> str:
        if self.busy:
            return "balayage déjà en cours"
        parts = [f"{len(self.written)} condensé(s) écrit(s)", f"{self.sessions} session(s) vue(s)"]
        if self.active:
            parts.append(f"{self.active} encore active(s)")
        for reason, count in sorted(self.excluded.items()):
            parts.append(f"{count} ignorée(s) : {reason}")
        if self.redactions:
            parts.append(f"{self.redactions} secret(s) masqué(s)")
        return ", ".join(parts)


def load_state(config: Config) -> dict:
    state = read_json(config.host_state, None)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        state = {"version": STATE_VERSION, "sessions": {}}
    state.setdefault("sessions", {})
    return state


def _relative(path: str, root: str) -> str:
    if root and (path == root or path.startswith(root.rstrip("/") + "/")):
        return path[len(root.rstrip("/")) + 1:] or "."
    return tilde(path)


def _scratch(path: str) -> bool:
    """Fichiers de brouillon des agents : sans intérêt pour la mémoire d'un projet."""
    return "/scratchpad/" in path or path.startswith(("/tmp/claude-", "/private/tmp/claude-"))


def _split(messages: list[Message], limit: int) -> list[list[Message]]:
    parts, current, size = [], [], 0
    for message in messages:
        if current and size + len(message.text) > limit:
            parts.append(current)
            current, size = [], 0
        current.append(message)
        size += len(message.text)
    if current:
        parts.append(current)
    return parts


class Sweeper:
    def __init__(self, config: Config):
        self.config = config
        self.adapters = adapters_mod.build(config)
        self.resolver = ProjectResolver()
        self.redactor = Redactor(config.list("MEMEX_REDACT_EXTRA"), config.bool("MEMEX_REDACT_EMAILS"))

    def run(self, force: Iterable[str] = (), force_all: bool = False, dry_run: bool = False, discard: bool = False) -> SweepReport:
        """`discard` : marque tout l'existant comme déjà vu, sans rien produire (pas de reprise d'historique)."""
        report = SweepReport()
        try:
            with file_lock(self.config.data_dir / "sweep.lock"):
                self._run(set(force), force_all or discard, dry_run, report, discard)
        except LockBusy:
            report.busy = True
        report.redactions = self.redactor.total
        return report

    def _run(self, force: set[str], force_all: bool, dry_run: bool, report: SweepReport, discard: bool = False) -> None:
        config = self.config
        state = load_state(config)
        sessions = state["sessions"]
        idle = config.int("MEMEX_IDLE_MINUTES") * 60
        marker = config.get("MEMEX_OPTOUT_MARKER").strip()
        clock = time.time()
        for adapter in self.adapters:
            for ref in adapter.discover():
                entry = sessions.get(ref.key) or {"cursor": 0, "seq": 0, "meta": {}}
                if ref.size < int(entry.get("cursor") or 0) and ref.path.suffix == ".jsonl":
                    # Fichier réécrit plus court (rotation, troncature) : le curseur ne veut plus rien dire.
                    entry["cursor"] = 0
                if ref.size <= int(entry.get("cursor") or 0):
                    continue
                report.sessions += 1
                if not (force_all or ref.session in force or clock - ref.mtime >= idle):
                    report.active += 1
                    continue
                try:
                    chunk = adapter.read(ref, entry.get("cursor"), entry.get("meta") or {})
                except OSError:
                    continue
                entry["cursor"], entry["meta"] = chunk.cursor, chunk.meta
                sessions[ref.key] = entry
                if chunk.skip:
                    entry["skip"] = chunk.skip
                if marker and any(marker in m.text for m in chunk.messages if m.role == "user"):
                    entry["skip"] = f"marqueur {marker}"
                if entry.get("skip"):
                    report.exclude(entry["skip"])
                    continue
                if discard:
                    entry["meta"]["files"], entry["meta"]["links"] = [], []
                    report.exclude("historique laissé de côté")
                    continue
                cwd =str(chunk.meta.get("cwd") or "")
                project = self.resolver.resolve(cwd)
                reason = exclusion_reason(
                    cwd, project,
                    config.list("MEMEX_EXCLUDE_PATHS"), config.list("MEMEX_EXCLUDE_REMOTES"),
                    config.list("MEMEX_INCLUDE_PATHS"),
                )
                if reason:
                    report.exclude(reason)
                    continue
                messages = self._prepare(chunk.messages)
                if not messages:
                    continue
                root = project.root if project else ""
                for part in _split(messages, max(config.int("MEMEX_MAX_SEGMENT_CHARS"), 2000)):
                    entry["seq"] = int(entry.get("seq") or 0) + 1
                    stamps = [m.ts for m in part if m.ts]
                    fallback = now()
                    segment = Segment(
                        source=adapter.name, device=config.device, session=ref.session, seq=entry["seq"],
                        messages=part,
                        started=min(stamps) if stamps else fallback,
                        ended=max(stamps) if stamps else fallback,
                        project=project.slug if project else "",
                        project_name=project.name if project else "",
                        project_remote=project.remote if project else "",
                        cwd=tilde(cwd), branch=str(chunk.meta.get("branch") or ""),
                        title=self.redactor(str(chunk.meta.get("title") or adapters_mod.first_title(part))),
                        files=[_relative(f, root) for f in (chunk.meta.get("files") or []) if not _scratch(f)],
                        links=list(chunk.meta.get("links") or []),
                    )
                    if not dry_run:
                        atomic_write(config.outbox / segment.filename, segment.render(), 0o600)
                    report.written.append(segment.filename)
                # Les fichiers touchés sont rattachés au condensé qui vient d'être écrit.
                entry["meta"]["files"], entry["meta"]["links"] = [], []
        if not dry_run:
            write_json(config.host_state, state)

    def _prepare(self, messages: list[Message]) -> list[Message]:
        config = self.config
        interim_min = config.int("MEMEX_INTERIM_MIN_CHARS")
        limit = config.int("MEMEX_MAX_MESSAGE_CHARS")
        kept = []
        for message in messages:
            if message.role == "assistant" and not message.final and len(message.text) < interim_min:
                continue
            text = truncate_middle(self.redactor(message.text), limit)
            kept.append(Message(message.role, text, message.ts, message.final))
        has_human = any(m.role == "user" for m in kept)
        if not has_human and sum(len(m.text) for m in kept) < 400:
            return []
        return kept


def write_note(config: Config, text: str, cwd: Optional[str] = None, title: str = "") -> Path:
    """Capture explicite : une note déposée directement dans la boîte d'envoi."""
    redactor = Redactor(config.list("MEMEX_REDACT_EXTRA"), config.bool("MEMEX_REDACT_EMAILS"))
    cwd = cwd or os.getcwd()
    project = ProjectResolver().resolve(cwd)
    stamp = now()
    body = redactor(text.strip())
    segment = Segment(
        source="note", device=config.device, session=uuid.uuid4().hex, seq=1,
        messages=[Message("user", "Note à consigner dans le wiki (capture explicite) :\n\n" + body, stamp)],
        started=stamp, ended=stamp,
        project=project.slug if project else "", project_name=project.name if project else "",
        project_remote=project.remote if project else "", cwd=tilde(cwd),
        title=redactor(title) or " ".join(body.split())[:90],
    )
    path = config.outbox / segment.filename
    atomic_write(path, segment.render(), 0o600)
    return path


def session_cursor(config: Config, key: str) -> dict:
    return load_state(config)["sessions"].get(key, {})


__all__ = ["Sweeper", "SweepReport", "write_note", "load_state", "UTC"]
