"""Adaptateurs : chaque source de sessions expose `discover()` et `read()`."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .. import PIPELINE_SENTINEL
from ..segment import Chunk, Message

# Blocs injectés par les harnais dans les messages « utilisateur » : pas des paroles humaines.
_WRAPPED = re.compile(
    r"<(system-reminder|local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr|task-notification|user-prompt-submit-hook|"
    r"bash-input|bash-stdout|bash-stderr|environment_context|user_instructions|"
    r"ide_opened_file|ide_selection|skills_instructions|permissions|"
    r"collaboration_mode|multi_agent_role|multi_agent_mode|tools)\b[^>]*>.*?</\1>",
    re.S,
)
_NOISE_PREFIX = ("[Request interrupted", "[Image:", "Caveat: The messages below")


def clean_human(text: str) -> str:
    text = _WRAPPED.sub("", text or "").strip()
    if not text or text.startswith(_NOISE_PREFIX):
        return ""
    return text


def is_pipeline(text: str) -> bool:
    return (text or "").lstrip().startswith(PIPELINE_SENTINEL)


@dataclass
class SessionRef:
    source: str
    session: str
    path: Path
    mtime: float
    size: int

    @property
    def key(self) -> str:
        return f"{self.source}:{self.session}"


class Adapter:
    name = ""

    def discover(self) -> Iterator[SessionRef]:
        raise NotImplementedError

    def read(self, ref: SessionRef, cursor: Any, meta: dict) -> Chunk:
        raise NotImplementedError


def mark_final(events: list[tuple]) -> list[Message]:
    """Transforme une suite d'événements en messages.

    Un texte d'assistant est « intermédiaire » s'il est suivi d'un appel d'outil
    avant la prochaine parole humaine : c'est en général de la narration.
    """
    messages: list[Message] = []
    for index, event in enumerate(events):
        kind = event[0]
        if kind == "human":
            messages.append(Message("user", event[2], event[1], True))
        elif kind == "assistant":
            final = True
            for later in events[index + 1:]:
                if later[0] == "human":
                    break
                if later[0] == "tool":
                    final = False
                    break
            if len(event) > 3 and event[3] is not None:
                final = bool(event[3])
            messages.append(Message("assistant", event[2], event[1], final))
    return messages


def build(config) -> list[Adapter]:
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .hermes import HermesAdapter

    known = {
        "claude-code": lambda: ClaudeCodeAdapter(config.path_value("MEMEX_CLAUDE_PROJECTS_DIR")),
        "codex": lambda: CodexAdapter(config.path_value("MEMEX_CODEX_SESSIONS_DIR")),
        "hermes": lambda: HermesAdapter(
            config.path_value("MEMEX_HERMES_DB"), config.list("MEMEX_HERMES_EXCLUDE_SOURCES")
        ),
    }
    adapters = []
    for name in config.list("MEMEX_SOURCES"):
        if name in known:
            adapters.append(known[name]())
    return adapters


def first_title(messages: list[Message], limit: int = 90) -> str:
    for message in messages:
        if message.role == "user":
            line = " ".join(message.text.split())
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def optional_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None
