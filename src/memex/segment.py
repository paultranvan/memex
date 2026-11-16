"""Le condensé : un extrait immuable de session, en markdown avec en-tête.

Un condensé (« segment ») contient les messages échangés depuis le balayage
précédent. Il n'est jamais modifié après écriture : la suite d'une session
donne un nouveau segment, numéroté.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import frontmatter
from .util import UTC, iso, parse_ts

FORMAT = "memex/segment-1"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}\.md$")
ROLE_LABEL = {"user": "Utilisateur", "assistant": "Assistant"}
_HEADING = re.compile(r"^## (Utilisateur|Assistant)(?: — (.*))?$")


@dataclass
class Message:
    role: str
    text: str
    ts: Optional[dt.datetime] = None
    final: bool = True


@dataclass
class Chunk:
    """Ce qu'un adaptateur a lu depuis le dernier curseur."""

    messages: list[Message] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    cursor: Any = None
    skip: str = ""


@dataclass
class Segment:
    source: str
    device: str
    session: str
    seq: int
    messages: list[Message]
    started: dt.datetime
    ended: dt.datetime
    project: str = ""
    project_name: str = ""
    project_remote: str = ""
    cwd: str = ""
    branch: str = ""
    title: str = ""
    files: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    path: Optional[Path] = None

    @property
    def id(self) -> str:
        return f"{self.source}_{self.session}_{self.seq:03d}"

    @property
    def filename(self) -> str:
        stamp = self.started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}_{self.id}.md"
        return re.sub(r"[^A-Za-z0-9._-]", "-", name)

    @property
    def user_chars(self) -> int:
        return sum(len(m.text) for m in self.messages if m.role == "user")

    @property
    def chars(self) -> int:
        return sum(len(m.text) for m in self.messages)

    def header(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "format": FORMAT,
            "id": self.id,
            "source": self.source,
            "device": self.device,
            "session": self.session,
            "seq": self.seq,
            "started": iso(self.started),
            "ended": iso(self.ended),
            "project": self.project,
            "project_name": self.project_name,
            "project_remote": self.project_remote,
            "cwd": self.cwd,
            "branch": self.branch,
            "title": self.title,
            "user_messages": sum(1 for m in self.messages if m.role == "user"),
            "user_chars": self.user_chars,
            "files": self.files[:40],
            "links": self.links[:20],
        }
        return {k: v for k, v in data.items() if v not in ("", [], None)}

    def render(self) -> str:
        parts = []
        for message in self.messages:
            stamp = iso(message.ts) if message.ts else ""
            label = ROLE_LABEL.get(message.role, message.role)
            parts.append(f"## {label}" + (f" — {stamp}" if stamp else "") + f"\n\n{_escape(message.text.strip())}\n")
        return frontmatter.dump(self.header(), "\n".join(parts))

    def transcript(self) -> str:
        """Le corps seul, tel qu'on le donne à lire au modèle."""
        return frontmatter.split(self.render())[1]


def _escape(text: str) -> str:
    """Un message ne doit pas pouvoir imiter un titre de tour de parole."""
    return re.sub(r"(?m)^## (Utilisateur|Assistant)\b", r"\\## \1", text)


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name)) and not name.startswith(".")


def load(path: Path) -> Segment:
    text = Path(path).read_text(encoding="utf-8")
    head, body = frontmatter.parse(text)
    if head.get("format") != FORMAT:
        raise ValueError(f"{path.name} : format de condensé inconnu ({head.get('format')!r})")
    messages: list[Message] = []
    role, stamp, lines = None, None, []

    def flush() -> None:
        if role:
            messages.append(Message(role=role, text="\n".join(lines).strip(), ts=parse_ts(stamp)))

    for line in body.split("\n"):
        match = _HEADING.match(line)
        if match:
            flush()
            role = "user" if match.group(1) == "Utilisateur" else "assistant"
            stamp, lines = match.group(2), []
        elif role:
            lines.append(line)
    flush()
    started = parse_ts(head.get("started")) or dt.datetime.fromtimestamp(Path(path).stat().st_mtime, UTC)
    return Segment(
        source=str(head.get("source", "")),
        device=str(head.get("device", "")),
        session=str(head.get("session", "")),
        seq=int(head.get("seq") or 1),
        messages=messages,
        started=started,
        ended=parse_ts(head.get("ended")) or started,
        project=str(head.get("project") or ""),
        project_name=str(head.get("project_name") or ""),
        project_remote=str(head.get("project_remote") or ""),
        cwd=str(head.get("cwd") or ""),
        branch=str(head.get("branch") or ""),
        title=str(head.get("title") or ""),
        files=frontmatter.as_list(head.get("files")),
        links=frontmatter.as_list(head.get("links")),
        path=Path(path),
    )
