"""Import of the official ChatGPT and claude.ai exports (.zip archive or conversations.json).

Experimental: written from the known structure of these exports, still to be
checked against a real one. Each conversation becomes a segment; a new export
only picks up the messages added since the previous import.
"""
from __future__ import annotations

import datetime as dt
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import Config
from .segment import Message, Segment
from .sweep import Sweeper, _split, load_state
from .util import UTC, atomic_write, now, parse_ts, write_json

KINDS = {"chatgpt": "chatgpt", "claude": "claude-ai"}


@dataclass
class ImportReport:
    conversations: int = 0
    written: list[str] = field(default_factory=list)
    skipped_short: int = 0
    skipped_old: int = 0

    def summary(self) -> str:
        return (
            f"{self.conversations} conversation(s) read, {len(self.written)} segment(s) written, "
            f"{self.skipped_short} too short, {self.skipped_old} older than the requested date"
        )


def read_conversations(path: Path) -> list[dict]:
    path = Path(path)
    chunks: list[Any] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = sorted(n for n in archive.namelist() if Path(n).name.startswith("conversations") and n.endswith(".json"))
            for name in names:
                chunks.append(json.loads(archive.read(name).decode("utf-8")))
    else:
        chunks.append(json.loads(path.read_text(encoding="utf-8")))
    result: list[dict] = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            chunk = chunk.get("conversations") or [chunk]
        result.extend(item for item in chunk if isinstance(item, dict))
    return result


def _chatgpt_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    if content.get("content_type") not in ("text", "multimodal_text"):
        return ""
    parts = []
    for part in content.get("parts") or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif isinstance(part, dict):
            parts.append("[attachment]")
    return "\n".join(p for p in parts if p).strip()


def chatgpt_messages(conversation: dict) -> Iterator[Message]:
    mapping = conversation.get("mapping") or {}
    chain, node = [], conversation.get("current_node")
    seen = set()
    while node and node in mapping and node not in seen:
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    if not chain:  # export without a current node: we take the chronological order
        chain = sorted(mapping.values(), key=lambda n: ((n.get("message") or {}).get("create_time") or 0), reverse=True)
    for entry in reversed(chain):
        message = entry.get("message") or {}
        role = (message.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        if (message.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        text = _chatgpt_text(message.get("content"))
        if text:
            yield Message(role, text, parse_ts(message.get("create_time")))


def claude_messages(conversation: dict) -> Iterator[Message]:
    for message in conversation.get("chat_messages") or []:
        if not isinstance(message, dict):
            continue
        role = {"human": "user", "assistant": "assistant"}.get(message.get("sender"))
        if not role:
            continue
        text = message.get("text") or ""
        if not text and isinstance(message.get("content"), list):
            text = "\n".join(b.get("text", "") for b in message["content"] if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            yield Message(role, text.strip(), parse_ts(message.get("created_at")))


def run(config: Config, kind: str, path: Path, since: Optional[dt.datetime] = None, min_chars: int = 400, dry_run: bool = False) -> ImportReport:
    source = KINDS[kind]
    report = ImportReport()
    state = load_state(config)
    sessions = state["sessions"]
    sweeper = Sweeper(config)
    limit = max(config.int("MEMEX_MAX_SEGMENT_CHARS"), 2000)
    for conversation in read_conversations(path):
        report.conversations += 1
        if kind == "chatgpt":
            ident = str(conversation.get("conversation_id") or conversation.get("id") or "")
            title = str(conversation.get("title") or "")
            updated = parse_ts(conversation.get("update_time") or conversation.get("create_time"))
            messages = list(chatgpt_messages(conversation))
        else:
            ident = str(conversation.get("uuid") or "")
            title = str(conversation.get("name") or "")
            updated = parse_ts(conversation.get("updated_at") or conversation.get("created_at"))
            messages = list(claude_messages(conversation))
        if not ident or not messages:
            continue
        if since and updated and updated < since:
            report.skipped_old += 1
            continue
        key = f"{source}:{ident}"
        entry = sessions.get(key) or {"cursor": 0, "seq": 0, "meta": {}}
        fresh = messages[int(entry.get("cursor") or 0):]
        if not fresh:
            continue
        if not entry.get("cursor") and sum(len(m.text) for m in messages if m.role == "user") < min_chars:
            report.skipped_short += 1
            continue
        prepared = sweeper._prepare(fresh)
        for part in _split(prepared, limit):
            entry["seq"] = int(entry.get("seq") or 0) + 1
            stamps = [m.ts for m in part if m.ts]
            fallback = updated or now()
            segment = Segment(
                source=source, device=config.device, session=ident, seq=entry["seq"], messages=part,
                started=min(stamps) if stamps else fallback, ended=max(stamps) if stamps else fallback,
                title=sweeper.redactor(title) or "untitled conversation",
            )
            if not dry_run:
                atomic_write(config.outbox / segment.filename, segment.render(), 0o600)
            report.written.append(segment.filename)
        entry["cursor"] = len(messages)
        sessions[key] = entry
    if not dry_run:
        write_json(config.host_state, state)
    return report


__all__ = ["run", "read_conversations", "chatgpt_messages", "claude_messages", "ImportReport", "UTC"]
