"""Lancement d'un agent (Hermes, Claude Code, Codex ou autre) pour une étape du pipeline.

Le pipeline ne dépend d'aucun harnais : il écrit une invite dans un fichier et
lance une commande shell. Trois niveaux de droits :

  - extract : aucun outil, texte en entrée, JSON en sortie ;
  - read    : lecture des fichiers du répertoire de travail ;
  - edit    : lecture et écriture dans le répertoire de travail.

Variables passées à la commande : MEMEX_PROMPT_FILE, MEMEX_OUT_FILE (réponse
finale, si le harnais sait l'écrire), MEMEX_MODEL.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import PIPELINE_SENTINEL
from .config import Config
from .util import now

_MODEL = '${MEMEX_MODEL:+--model "$MEMEX_MODEL"}'
_CODEX = 'codex exec --skip-git-repo-check --ephemeral -o "$MEMEX_OUT_FILE" ${MEMEX_MODEL:+-m "$MEMEX_MODEL"}'
# Hermes ancre ses outils de fichiers sur TERMINAL_CWD, pas sur le répertoire du processus.
_HERMES = 'TERMINAL_CWD="$PWD" hermes --ignore-rules ${MEMEX_MODEL:+-m "$MEMEX_MODEL"}'

PRESETS: dict[str, dict[str, str]] = {
    "claude": {
        "extract": f'claude -p --tools "" --no-session-persistence {_MODEL} < "$MEMEX_PROMPT_FILE"',
        "read": f'claude -p --tools "Read,Glob,Grep" --no-session-persistence {_MODEL} < "$MEMEX_PROMPT_FILE"',
        "edit": f'claude -p --tools "Read,Write,Edit,Glob,Grep" --permission-mode acceptEdits --no-session-persistence {_MODEL} < "$MEMEX_PROMPT_FILE"',
    },
    "codex": {
        "extract": f'{_CODEX} --sandbox read-only - < "$MEMEX_PROMPT_FILE" > /dev/null',
        "read": f'{_CODEX} --sandbox read-only - < "$MEMEX_PROMPT_FILE" > /dev/null',
        "edit": f'{_CODEX} --sandbox workspace-write - < "$MEMEX_PROMPT_FILE" > /dev/null',
    },
    "hermes": {
        "extract": f'{_HERMES} -t todo -z "$(cat "$MEMEX_PROMPT_FILE")"',
        "read": f'{_HERMES} -t file -z "$(cat "$MEMEX_PROMPT_FILE")"',
        "edit": f'{_HERMES} -t file --yolo -z "$(cat "$MEMEX_PROMPT_FILE")"',
    },
}
# Les harnais qui reçoivent l'invite en argument sont limités par le noyau (128 Ko par argument).
ARGV_PRESETS = {"hermes"}
ARGV_LIMIT = 120_000


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    returncode: int
    seconds: float
    log: Optional[Path] = None


class Agent:
    def __init__(self, config: Config):
        self.config = config
        self.name = (config.get("MEMEX_AGENT") or "hermes").strip().lower()
        self.timeout = config.int("MEMEX_AGENT_TIMEOUT_S")

    def command(self, kind: str) -> str:
        override = self.config.get(f"MEMEX_AGENT_{kind.upper()}_CMD").strip()
        if override:
            return override
        if self.name not in PRESETS:
            raise AgentError(
                f"MEMEX_AGENT={self.name!r} inconnu : choisis hermes, claude ou codex, "
                f"ou définis MEMEX_AGENT_{kind.upper()}_CMD"
            )
        return PRESETS[self.name][kind]

    # ── bac à sable ───────────────────────────────────────────────────────
    def sandbox(self, kind: str, cwd: Path, tmp: Path) -> list[str]:
        """Préfixe bubblewrap : tout le système en lecture seule, sauf le dossier de
        travail (pour `edit`) et l'état propre du harnais. Les clés SSH sont masquées.

        MEMEX_SANDBOX : `auto` (défaut, utilisé s'il fonctionne), `bwrap` (exigé), `none`.
        """
        mode = (self.config.get("MEMEX_SANDBOX") or "auto").strip().lower()
        if mode == "none":
            return []
        binary = shutil.which("bwrap")
        base = [binary or "bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp"]
        if not binary or not self._sandbox_works(tuple(base)):
            if mode == "bwrap":
                raise AgentError("MEMEX_SANDBOX=bwrap mais bubblewrap est absent ou inutilisable ici")
            return []
        home = Path.home()
        writable = self.config.list("MEMEX_SANDBOX_RW") or [
            ".hermes", ".claude", ".claude.json", ".codex", ".cache", ".config", ".local/state", ".local/share/claude", ".npm",
        ]
        args = list(base)
        for name in writable:
            path = home / name
            if path.exists():
                args += ["--bind", str(path), str(path)]
        for name in (".ssh", ".gnupg"):
            if (home / name).is_dir():
                args += ["--tmpfs", str(home / name)]
        args += ["--bind", str(tmp), str(tmp)]
        args += ["--bind" if kind == "edit" else "--ro-bind", str(cwd), str(cwd), "--chdir", str(cwd)]
        return args

    _sandbox_cache: dict[tuple, bool] = {}

    @classmethod
    def _sandbox_works(cls, base: tuple) -> bool:
        if base not in cls._sandbox_cache:
            try:
                ok = subprocess.run([*base, "true"], capture_output=True, timeout=20).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            cls._sandbox_cache[base] = ok
        return cls._sandbox_cache[base]

    def sandbox_state(self) -> str:
        mode = (self.config.get("MEMEX_SANDBOX") or "auto").strip().lower()
        if mode == "none":
            return "désactivé (MEMEX_SANDBOX=none)"
        binary = shutil.which("bwrap")
        if not binary:
            return "indisponible : bubblewrap n'est pas installé"
        base = (binary, "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp")
        return "actif (bubblewrap)" if self._sandbox_works(base) else "indisponible : bubblewrap ne démarre pas dans cet environnement"

    def run(self, kind: str, prompt: str, cwd: Path, label: str = "") -> AgentResult:
        if not prompt.startswith(PIPELINE_SENTINEL):
            prompt = f"{PIPELINE_SENTINEL}\n{prompt}"
        command = self.command(kind)
        uses_argv = "$(cat" in command
        if uses_argv and len(prompt.encode("utf-8")) > ARGV_LIMIT:
            raise AgentError("invite trop longue pour ce harnais (réduis MEMEX_MAX_SEGMENT_CHARS)")
        logs = self.config.logs / "agent"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = now().strftime("%Y%m%dT%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label)[:80] or "run"
        log_path = logs / f"{stamp}-{kind}-{safe}.log"
        with tempfile.TemporaryDirectory(prefix="memex-agent-") as tmp:
            prompt_file, out_file = Path(tmp) / "prompt.md", Path(tmp) / "reponse.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "MEMEX_PROMPT_FILE": str(prompt_file), "MEMEX_OUT_FILE": str(out_file),
                "MEMEX_MODEL": self.config.get("MEMEX_AGENT_MODEL"), "MEMEX_PIPELINE": "1",
            })
            started = time.time()
            proc = subprocess.Popen(
                [*self.sandbox(kind, Path(cwd), Path(tmp)), "sh", "-c", command], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                stdout, stderr = proc.communicate()
                self._log(log_path, kind, command, prompt, stdout, stderr, "délai dépassé")
                raise AgentError(f"l'agent a dépassé {self.timeout} s ({kind})")
            seconds = time.time() - started
            text = stdout or ""
            try:
                produced = out_file.read_text(encoding="utf-8")
                if produced.strip():
                    text = produced
            except OSError:
                pass
        self._log(log_path, kind, command, prompt, text, stderr, f"code {proc.returncode}, {seconds:.0f} s")
        self._rotate(logs)
        if proc.returncode != 0:
            tail = (stderr or text or "").strip()[-400:]
            raise AgentError(f"l'agent a échoué (code {proc.returncode}) : {tail}")
        return AgentResult(text=text, returncode=proc.returncode, seconds=seconds, log=log_path)

    @staticmethod
    def _log(path: Path, kind: str, command: str, prompt: str, out: str, err: str, status: str) -> None:
        body = (
            f"# {kind} — {status}\n$ {command}\n\n## invite ({len(prompt)} caractères)\n{prompt[:6000]}\n"
            f"\n## sortie\n{(out or '')[-6000:]}\n\n## erreurs\n{(err or '')[-3000:]}\n"
        )
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _rotate(folder: Path, keep: int = 400) -> None:
        files = sorted(folder.glob("*.log"))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)


def parse_json_reply(text: str) -> Any:
    """Extrait l'objet JSON d'une réponse, même entourée de texte ou de clôtures de code."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    candidates.append(text)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise AgentError("la réponse de l'agent n'est pas un objet JSON")
