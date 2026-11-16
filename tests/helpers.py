"""Bac à sable de test : un « hôte » et un « serveur » dans un dossier temporaire."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from memex.config import Config  # noqa: E402

FAKE = f"{sys.executable} {ROOT / 'tests' / 'fake_agent.py'}"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class Sandbox(unittest.TestCase):
    """Chaque test reçoit un hôte et un serveur vierges, reliés en mode `local`."""

    overrides: dict = {}
    init_server = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="memex-test-")
        self.root = Path(self._tmp.name).resolve()
        self.projects = self.root / "claude-projects"
        self.codex = self.root / "codex-sessions"
        self.server_home = self.root / "server"
        self.vault = self.root / "vault"
        self.data = self.root / "hostdata"
        values = {
            "MEMEX_DATA_DIR": str(self.data), "MEMEX_DEVICE": "portable",
            "MEMEX_CLAUDE_PROJECTS_DIR": str(self.projects), "MEMEX_CODEX_SESSIONS_DIR": str(self.codex),
            "MEMEX_HERMES_DB": str(self.root / "hermes.db"), "MEMEX_SOURCES": "claude-code,codex",
            "MEMEX_IDLE_MINUTES": "0", "MEMEX_SERVER_SSH": "local", "MEMEX_HOME": str(self.server_home),
            "MEMEX_VAULT_DIR": str(self.vault), "MEMEX_TZ": "UTC", "MEMEX_TISSEUR_MIN_PAGES": "1",
            "MEMEX_AGENT_EXTRACT_CMD": FAKE, "MEMEX_AGENT_READ_CMD": FAKE, "MEMEX_AGENT_EDIT_CMD": FAKE,
            "MEMEX_MIN_USER_CHARS": "20", "MEMEX_BACKUP_PULL": "0",
        }
        values.update(self.overrides)
        self.env_file = self.root / "memex.env"
        self.write_config(values)
        self._saved = {k: os.environ.get(k) for k in ("MEMEX_CONFIG", "FAKE_AGENT_MODE", "FAKE_AGENT_CALLS")}
        os.environ["MEMEX_CONFIG"] = str(self.env_file)
        os.environ.pop("FAKE_AGENT_MODE", None)
        for key in list(os.environ):
            if key.startswith("MEMEX_") and key != "MEMEX_CONFIG":
                del os.environ[key]
        self.calls = self.root / "agent-calls.log"
        os.environ["FAKE_AGENT_CALLS"] = str(self.calls)
        if self.init_server:
            from memex.pipeline import Server

            Server(self.config).init()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def write_config(self, values):
        self.values = dict(values)
        self.env_file.write_text("".join(f'{k}="{v}"\n' for k, v in self.values.items()), encoding="utf-8")

    def set(self, **changes):
        self.values.update(changes)
        self.write_config(self.values)

    @property
    def config(self) -> Config:
        return Config.load(self.env_file)

    def agent_calls(self) -> list:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    # ── fabrique de faux projets et de fausses sessions ───────────────────
    def make_repo(self, name: str, remote: str = "") -> Path:
        path = self.root / "code" / name
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        if remote:
            git(path, "remote", "add", "origin", remote)
        return path

    def claude_session(self, cwd, turns, session=None, start="2026-11-12T09:00:00Z", title="", append=False) -> str:
        """turns : ("user", texte) | ("assistant", texte) | ("tool", chemin) | ("raw", dict)"""
        session = session or str(uuid.uuid4())
        folder = self.projects / str(cwd).replace("/", "-")
        folder.mkdir(parents=True, exist_ok=True)
        base = {"isSidechain": False, "cwd": str(cwd), "gitBranch": "main", "sessionId": session, "version": "2.1.0"}
        lines = [] if append else [{"type": "mode", "mode": "normal", "sessionId": session}]
        for index, turn in enumerate(turns):
            stamp = start[:14] + f"{index % 60:02d}:00Z"
            head = {"parentUuid": str(uuid.uuid4())}
            if turn[0] == "user":
                lines.append({**head, **base, "type": "user", "message": {"role": "user", "content": turn[1]},
                              "origin": {"kind": "human"}, "promptSource": "typed", "timestamp": stamp})
            elif turn[0] == "assistant":
                lines.append({**head, **base, "type": "assistant", "timestamp": stamp,
                              "message": {"role": "assistant", "content": [{"type": "text", "text": turn[1]}]}})
            elif turn[0] == "tool":
                lines.append({**head, **base, "type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": turn[1]}}]}})
                lines.append({**head, **base, "type": "user", "timestamp": stamp, "toolUseResult": {"ok": True},
                              "message": {"role": "user", "content": [{"type": "tool_result", "content": "fait"}]}})
            elif turn[0] == "raw":
                lines.append({**head, **base, "timestamp": stamp, **turn[1]})
        if title:
            lines.append({"type": "ai-title", "aiTitle": title, "sessionId": session})
        with open(folder / f"{session}.jsonl", "a" if append else "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        return session

    def codex_session(self, cwd, turns, session=None, start="2026-11-12T10:00:00Z", thread="user") -> str:
        session = session or str(uuid.uuid4())
        folder = self.codex / "2026" / "11" / "12"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [{"timestamp": start, "type": "session_meta", "payload": {
            "id": session, "cwd": str(cwd), "source": "cli", "thread_source": thread, "cli_version": "0.169.0"}}]
        for index, turn in enumerate(turns):
            stamp = start[:14] + f"{index % 60:02d}:00Z"
            if turn[0] == "user":
                item = {"type": "UserMessage", "id": str(index), "content": [{"type": "text", "text": turn[1]}]}
            elif turn[0] == "assistant":
                item = {"type": "AgentMessage", "id": str(index), "phase": turn[2] if len(turn) > 2 else "final_answer",
                        "content": [{"type": "Text", "text": turn[1]}]}
            else:
                item = {"type": "FileChange", "id": str(index), "changes": {turn[1]: {}}}
            lines.append({"timestamp": stamp, "type": "event_msg", "payload": {"type": "item_completed", "item": item}})
            if turn[0] == "user":  # bruit injecté par le harnais, à ne pas confondre avec une parole humaine
                lines.append({"timestamp": stamp, "type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}})
        path = folder / f"rollout-2026-11-12T10-00-00-{session}.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return session
