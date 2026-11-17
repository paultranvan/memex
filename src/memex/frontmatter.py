"""YAML front matter of the pages: lenient reading of a subset, Obsidian-compatible writing.

No PyYAML dependency. We cover what the wiki uses: scalars, inline lists
`[a, b]` and block lists `- a`. Everything else is kept as text.
"""
from __future__ import annotations

import json
import re
from typing import Any

_KEY = re.compile(r"^([A-Za-zÀ-ÿ_][\wÀ-ÿ .-]*?)\s*:\s?(.*)$")
_PLAIN = re.compile(r"^[A-Za-zÀ-ÿ0-9_][A-Za-zÀ-ÿ0-9 _./()'’+@%&=!?-]*$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def split(text: str) -> tuple[str, str]:
    """Separates the raw header (without the `---`) from the body. Empty header if there is none."""
    if not text.startswith("---"):
        return "", text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:]).lstrip("\n")
    return "", text


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if value[0] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value.strip('"')
    if value[0] == "'" and value[-1] == "'" and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _inline_list(raw: str) -> list[Any]:
    inner = raw.strip()[1:-1]
    items, current, quote, depth = [], "", "", 0
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current += char
        elif char == "[":
            depth += 1
            current += char
        elif char == "]":
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current)
    return [_scalar(item) for item in items if item.strip()]


def parse_header(header: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    key = None
    for line in header.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key is not None and (line[0] in " \t-"):
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_scalar(item.group(1)))
            continue
        match = _KEY.match(line) if not line[0].isspace() else None
        if not match:
            if key is not None and isinstance(data.get(key), str):
                data[key] = (data[key] + " " + line.strip()).strip()
            continue
        key, raw = match.group(1).strip(), match.group(2).strip()
        if raw.startswith("[") and raw.endswith("]") and not raw.startswith("[["):
            data[key] = _inline_list(raw)
        elif raw in ("", "|", ">", "|-", ">-"):
            data[key] = ""
        else:
            data[key] = _scalar(raw)
    return data


def parse(text: str) -> tuple[dict[str, Any], str]:
    header, body = split(text)
    return (parse_header(header) if header else {}), body


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        _PLAIN.match(text)
        and text.lower() not in _RESERVED
        and not re.fullmatch(r"-?\d+(\.\d+)?", text)
        and ": " not in text
        and not text.endswith(":")
        and text == text.strip()
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def dump_header(data: dict[str, Any]) -> str:
    lines = []
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {_dump_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_dump_scalar(value)}")
    return "\n".join(lines)


def dump(data: dict[str, Any], body: str) -> str:
    body = body.lstrip("\n")
    if not data:
        return body
    return f"---\n{dump_header(data)}\n---\n\n{body}" if body else f"---\n{dump_header(data)}\n---\n"


_TOP_KEY = re.compile(r"^([^\s#-][^:]*?)\s*:(\s|$)")


def set_keys(text: str, updates: dict[str, Any]) -> str:
    """Sets or replaces header fields without rewriting the other lines.

    A page's header may come from Obsidian or from a template and hold YAML that
    this module cannot read back faithfully (nested blocks, comments): so we
    leave it alone and only replace the requested fields.
    """
    if not updates:
        return text
    lines = text.split("\n")
    close = None
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if close is None:
        return dump(dict(updates), text)
    head = lines[1:close]
    for key, value in updates.items():
        fresh = dump_header({key: value}).split("\n")
        start = None
        for index, line in enumerate(head):
            match = _TOP_KEY.match(line)
            if match and match.group(1).strip() == key:
                start = index
                break
        if start is None:
            while head and not head[-1].strip():
                head.pop()
            head.extend(fresh)
            continue
        end = start + 1
        while end < len(head) and not (_TOP_KEY.match(head[end]) or head[end].startswith("#")):
            end += 1
        while end > start + 1 and not head[end - 1].strip():
            end -= 1
        head[start:end] = fresh
    return "\n".join(lines[:1] + head + lines[close:])


def as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]
