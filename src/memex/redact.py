"""Masquage des secrets avant qu'un condensé ne quitte la machine.

Par motifs connus plutôt que par entropie : mieux vaut masquer un peu trop
(`token = [MASQUÉ]`) que laisser passer une clé.
"""
from __future__ import annotations

import re
from typing import Iterable

MASK = "[MASQUÉ]"

_SECRET_KEY = r"[A-Za-z0-9_.-]*(?:passw(?:or)?d|passphrase|secret|token|api[_-]?key|apikey|private[_-]?key|access[_-]?key|client[_-]?secret|auth[_-]?key)[A-Za-z0-9_.-]*"

_RULES: list[tuple[str, re.Pattern, str]] = [
    ("cle-privee", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S), MASK),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"), MASK),
    ("cle-api", re.compile(r"\b(?:sk|rk|pk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"), MASK),
    ("stripe", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"), MASK),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), MASK),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), MASK),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MASK),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), MASK),
    ("tailscale", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{10,}"), MASK),
    ("telegram", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), MASK),
    ("url-identifiants", re.compile(r"\b([a-z][a-z0-9+.-]{1,15}://[^\s:/@]{1,64}):[^\s@/]{1,200}@"), r"\1:" + MASK + "@"),
    ("authorization", re.compile(r"(?i)\b(authorization\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + MASK),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"), r"\1" + MASK),
    (
        "affectation",
        re.compile(r"(?i)(?<![A-Za-z0-9_.-])(" + _SECRET_KEY + r")(\"?\s*[:=]\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"',;)}\]]{6,})"),
        r"\1\2" + MASK,
    ),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b"), "[IBAN]"),
    ("carte", re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b"), "[CARTE]"),
]

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class Redactor:
    def __init__(self, extra: Iterable[str] = (), emails: bool = False):
        self.rules = list(_RULES)
        for index, pattern in enumerate(extra):
            try:
                self.rules.append((f"perso-{index}", re.compile(pattern), MASK))
            except re.error:
                continue
        if emails:
            self.rules.append(("email", _EMAIL, "[EMAIL]"))
        self.hits: dict[str, int] = {}

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for name, pattern, replacement in self.rules:
            text, count = pattern.subn(replacement, text)
            if count:
                self.hits[name] = self.hits.get(name, 0) + count
        return text

    @property
    def total(self) -> int:
        return sum(self.hits.values())
