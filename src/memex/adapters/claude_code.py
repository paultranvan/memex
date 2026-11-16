"""Transcripts Claude Code : ~/.claude/projects/<projet>/<session>.jsonl

Lecture incrémentale par octets. Les sous-agents (dossier `subagents/`) et les
lignes techniques (requêtes API, instantanés) sont ignorés.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ..segment import Chunk
from ..util import parse_ts
from . import Adapter, SessionRef, clean_human, is_pipeline, mark_final

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Lignes volumineuses et sans paroles : écartées sans même les décoder. Tout type inconnu est lu.
_TECHNICAL = tuple(b'{"type":"' + name + b'"' for name in (
    b"api-request", b"api-request-blob", b"api-request-shape", b"file-history-snapshot", b"mode",
    b"permission-mode", b"session-owner", b"atis-latch", b"last-prompt", b"tools-used", b"queue-operation",
    b"cost-state", b"bridge-session", b"summary", b"instructions", b"attachment", b"system",
))


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def __init__(self, projects_dir: Path):
        self.root = Path(projects_dir)

    def discover(self) -> Iterator[SessionRef]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*/*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            yield SessionRef(self.name, path.stem, path, stat.st_mtime, stat.st_size)

    def read(self, ref: SessionRef, cursor: Any, meta: dict) -> Chunk:
        offset = int(cursor or 0)
        meta = dict(meta or {})
        files = list(meta.get("files") or [])
        links = list(meta.get("links") or [])
        events: list[tuple] = []
        skip = ""
        with open(ref.path, "rb") as handle:
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break  # ligne en cours d'écriture : on la reprendra au prochain passage
                offset += len(raw)
                if raw.startswith(_TECHNICAL):
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain"):
                    continue
                kind = entry.get("type")
                if kind == "ai-title":
                    if entry.get("aiTitle"):
                        meta["title"] = str(entry["aiTitle"])
                    continue
                if kind == "pr-link":
                    if entry.get("prUrl") and entry["prUrl"] not in links:
                        links.append(entry["prUrl"])
                    continue
                if kind not in ("user", "assistant"):
                    continue
                if entry.get("cwd"):
                    meta.setdefault("cwd", entry["cwd"])
                if entry.get("gitBranch"):
                    meta["branch"] = entry["gitBranch"]
                stamp = parse_ts(entry.get("timestamp"))
                content = (entry.get("message") or {}).get("content")
                if kind == "assistant":
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text" and (block.get("text") or "").strip():
                            events.append(("assistant", stamp, block["text"].strip()))
                        elif block.get("type") == "tool_use":
                            events.append(("tool",))
                            target = (block.get("input") or {}).get("file_path")
                            if block.get("name") in _EDIT_TOOLS and isinstance(target, str) and target not in files:
                                files.append(target)
                    continue
                # ── tour « utilisateur » : parole humaine, résultat d'outil ou injection ──
                texts = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            events.append(("tool",))
                        elif block.get("type") == "text":
                            texts.append(block.get("text") or "")
                        elif block.get("type") == "image":
                            texts.append("[image jointe]")
                if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
                    continue
                origin = entry.get("origin")
                if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                    continue
                if entry.get("promptSource") == "system" or entry.get("toolUseResult"):
                    continue
                text = clean_human("\n".join(texts))
                if not text or text == "[image jointe]":
                    continue
                if is_pipeline(text):
                    skip = "session du pipeline memex"
                events.append(("human", stamp, text))
        meta["files"], meta["links"] = files[-60:], links[-20:]
        return Chunk(messages=mark_final(events), meta=meta, cursor=offset, skip=skip)
