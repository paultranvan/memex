"""Sessions Codex : ~/.codex/sessions/AAAA/MM/JJ/rollout-*.jsonl

Les paroles humaines et les réponses viennent des événements `item_completed`
(UserMessage / AgentMessage). Les messages `response_item` de rôle `user`
contiennent surtout des instructions injectées : on ne s'y fie pas.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from ..segment import Chunk
from ..util import parse_ts
from . import Adapter, SessionRef, clean_human, is_pipeline, mark_final

_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def _texts(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, sessions_dir: Path):
        self.root = Path(sessions_dir)

    def discover(self) -> Iterator[SessionRef]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("rollout-*.jsonl")):
            match = _ID.search(path.name)
            try:
                stat = path.stat()
            except OSError:
                continue
            yield SessionRef(self.name, match.group(1) if match else path.stem, path, stat.st_mtime, stat.st_size)

    def read(self, ref: SessionRef, cursor: Any, meta: dict) -> Chunk:
        offset = int(cursor or 0)
        meta = dict(meta or {})
        files = list(meta.get("files") or [])
        items: list[tuple] = []   # format courant (item_completed)
        legacy: list[tuple] = []  # anciens événements user_message / agent_message
        skip = meta.get("skip", "")
        with open(ref.path, "rb") as handle:
            handle.seek(offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                payload = entry.get("payload") if isinstance(entry, dict) else None
                if not isinstance(payload, dict):
                    continue
                kind = entry.get("type")
                stamp = parse_ts(entry.get("timestamp"))
                if kind == "session_meta":
                    if payload.get("cwd"):
                        meta.setdefault("cwd", payload["cwd"])
                    source, thread = payload.get("source"), payload.get("thread_source")
                    if isinstance(source, dict) or (thread and thread != "user"):
                        skip = meta["skip"] = "session interne de Codex (sous-agent ou relecture)"
                    continue
                if kind == "turn_context" and payload.get("cwd"):
                    meta.setdefault("cwd", payload["cwd"])
                    continue
                if kind != "event_msg":
                    continue
                event = payload.get("type")
                if event == "item_completed":
                    item = payload.get("item") or {}
                    item_type = item.get("type")
                    if item_type == "UserMessage":
                        text = clean_human(_texts(item.get("content")))
                        if text:
                            items.append(("human", stamp, text))
                    elif item_type == "AgentMessage":
                        text = _texts(item.get("content")).strip()
                        if text:
                            items.append(("assistant", stamp, text, item.get("phase") != "commentary"))
                    elif item_type == "FileChange":
                        items.append(("tool",))
                        for changed in (item.get("changes") or {}):
                            if isinstance(changed, str) and changed not in files:
                                files.append(changed)
                    elif item_type in ("CommandExecution", "Extension"):
                        items.append(("tool",))
                elif event == "user_message":
                    text = clean_human(str(payload.get("message") or ""))
                    if text:
                        legacy.append(("human", stamp, text))
                elif event == "agent_message":
                    text = str(payload.get("message") or "").strip()
                    if text:
                        legacy.append(("assistant", stamp, text, payload.get("phase") != "commentary"))
        events = items if any(e[0] == "human" for e in items) or not legacy else legacy
        if any(e[0] == "human" and is_pipeline(e[2]) for e in events):
            skip = meta["skip"] = "session du pipeline memex"
        meta["files"] = files[-60:]
        return Chunk(messages=mark_final(events), meta=meta, cursor=offset, skip=skip)
