"""Configuration : un fichier `KEY=VALUE` commun à l'hôte et au serveur.

Ordre de résolution : variable d'environnement > fichier > valeur par défaut.
Le fichier est lu par ce module, jamais exécuté : ne pas le charger dans un shell.
"""
from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Optional

from .util import split_list

DEFAULTS: dict[str, str] = {
    # ── commun ────────────────────────────────────────────────────────────
    "MEMEX_LANG": "fr",
    "MEMEX_TZ": "Europe/Paris",
    # ── hôte : capture ────────────────────────────────────────────────────
    "MEMEX_DEVICE": "",
    "MEMEX_DATA_DIR": "~/.local/share/memex",
    "MEMEX_SOURCES": "claude-code,codex",
    "MEMEX_CLAUDE_PROJECTS_DIR": "~/.claude/projects",
    "MEMEX_CODEX_SESSIONS_DIR": "~/.codex/sessions",
    "MEMEX_HERMES_DB": "~/.hermes/state.db",
    "MEMEX_HERMES_EXCLUDE_SOURCES": "cron",
    "MEMEX_IDLE_MINUTES": "10",
    "MEMEX_INTERIM_MIN_CHARS": "280",
    "MEMEX_MAX_MESSAGE_CHARS": "6000",
    "MEMEX_MAX_SEGMENT_CHARS": "40000",
    "MEMEX_EXCLUDE_PATHS": "",
    "MEMEX_EXCLUDE_REMOTES": "",
    "MEMEX_INCLUDE_PATHS": "",
    "MEMEX_OPTOUT_MARKER": "#memex-off",
    "MEMEX_REDACT_EXTRA": "",
    "MEMEX_REDACT_EMAILS": "0",
    # ── hôte : liaison avec le serveur ────────────────────────────────────
    "MEMEX_SERVER_SSH": "",
    "MEMEX_SERVER_HOME": "/srv/memex",
    "MEMEX_SSH_CMD": "ssh",
    "MEMEX_SSH_OPTS": "-o BatchMode=yes -o ConnectTimeout=6",
    "MEMEX_NET_TIMEOUT_S": "60",
    "MEMEX_SYNC_MIN_INTERVAL_S": "90",
    "MEMEX_BACKUP_PULL": "1",
    "MEMEX_BACKUP_KEEP": "7",
    # ── hôte : lecture par les agents ─────────────────────────────────────
    "MEMEX_HOOK_MAX_CHARS": "6000",
    # ── serveur ───────────────────────────────────────────────────────────
    "MEMEX_HOME": "/srv/memex",
    "MEMEX_VAULT_DIR": "",
    "MEMEX_AGENT": "hermes",
    "MEMEX_AGENT_MODEL": "",
    "MEMEX_AGENT_EXTRACT_CMD": "",
    "MEMEX_AGENT_READ_CMD": "",
    "MEMEX_AGENT_EDIT_CMD": "",
    "MEMEX_AGENT_TIMEOUT_S": "900",
    "MEMEX_SANDBOX": "auto",
    "MEMEX_SANDBOX_RW": "",
    "MEMEX_PROMPTS_DIR": "",
    "MEMEX_GATE_BIN": "",
    "MEMEX_AUTHORIZED_KEYS": "",
    "MEMEX_NOTIFY_CMD": "",
    "MEMEX_NOTIFY_LEVEL": "changes",
    "MEMEX_BATCH_MAX": "25",
    "MEMEX_RUN_BUDGET_S": "2400",
    "MEMEX_MIN_USER_CHARS": "80",
    "MEMEX_MAX_RETRIES": "3",
    "MEMEX_MAX_PAGE_CHARS": "24000",
    "MEMEX_TISSEUR": "1",
    "MEMEX_TISSEUR_MIN_PAGES": "12",
    "MEMEX_TISSEUR_MIN_DOSSIERS": "2",
    "MEMEX_VERIF_MAX_ACTIONS": "8",
    "MEMEX_REVIEW_TTL_DAYS": "10",
    "MEMEX_SERVER_BACKUP_KEEP": "14",
}

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _LINE.match(raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        values[key] = value
    return values


def default_config_paths() -> list[Path]:
    paths = []
    if os.environ.get("MEMEX_CONFIG"):
        paths.append(Path(os.environ["MEMEX_CONFIG"]).expanduser())
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    paths.append(Path(xdg).expanduser() / "memex" / "memex.env")
    paths.append(Path("/etc/memex/memex.env"))
    return paths


class Config:
    def __init__(self, values: Optional[dict[str, str]] = None, path: Optional[Path] = None):
        self.path = path
        self._values = dict(DEFAULTS)
        if values:
            self._values.update(values)

    @classmethod
    def load(cls, path: Optional[str | Path] = None, env: Optional[dict] = None) -> "Config":
        env = os.environ if env is None else env
        candidates = [Path(path).expanduser()] if path else default_config_paths()
        found, values = None, {}
        for candidate in candidates:
            try:
                values = parse_env_text(candidate.read_text(encoding="utf-8"))
                found = candidate
                break
            except OSError:
                continue
        for key, value in env.items():
            if key.startswith("MEMEX_") and key != "MEMEX_CONFIG":
                values[key] = value
        return cls(values, found)

    # ── accès bruts ───────────────────────────────────────────────────────
    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def int(self, key: str) -> int:
        try:
            return int(self.get(key) or DEFAULTS.get(key, "0"))
        except ValueError:
            return int(DEFAULTS.get(key, "0") or 0)

    def bool(self, key: str) -> bool:
        return self.get(key).strip().lower() in {"1", "true", "yes", "on", "oui"}

    def list(self, key: str) -> list[str]:
        return split_list(self.get(key))

    def path_value(self, key: str) -> Path:
        return Path(os.path.expandvars(self.get(key))).expanduser()

    # ── hôte ──────────────────────────────────────────────────────────────
    @property
    def device(self) -> str:
        name = self.get("MEMEX_DEVICE") or socket.gethostname().split(".")[0]
        return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:64] or "device"

    @property
    def data_dir(self) -> Path:
        return self.path_value("MEMEX_DATA_DIR")

    @property
    def outbox(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def sent(self) -> Path:
        return self.data_dir / "sent"

    @property
    def wiki_clone(self) -> Path:
        return self.data_dir / "wiki"

    @property
    def host_state(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def server_ssh(self) -> str:
        return self.get("MEMEX_SERVER_SSH").strip()

    @property
    def is_local_server(self) -> bool:
        return self.server_ssh.lower() == "local"

    # ── serveur ───────────────────────────────────────────────────────────
    @property
    def home(self) -> Path:
        return self.path_value("MEMEX_HOME")

    @property
    def wiki_repo(self) -> Path:
        return self.home / "wiki"

    @property
    def inbox(self) -> Path:
        return self.home / "raw" / "inbox"

    @property
    def archive(self) -> Path:
        return self.home / "raw" / "archive"

    @property
    def extracts(self) -> Path:
        return self.home / "raw" / "extracts"

    @property
    def quarantine(self) -> Path:
        return self.home / "raw" / "quarantine"

    @property
    def trigger(self) -> Path:
        return self.home / "raw" / "trigger"

    @property
    def work(self) -> Path:
        return self.home / "work"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def vault_dir(self) -> Optional[Path]:
        return self.path_value("MEMEX_VAULT_DIR") if self.get("MEMEX_VAULT_DIR").strip() else None
