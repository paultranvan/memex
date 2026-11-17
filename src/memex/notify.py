"""Notifications: a configurable shell command, which receives the message on its standard input.

Example with Hermes: MEMEX_NOTIFY_CMD='hermes send --to telegram --subject "$MEMEX_SUBJECT" --file -'
With no command configured, the message is only written to the logs.
"""
from __future__ import annotations

import os
import subprocess

from .config import Config
from .util import append_line, eprint, iso


def notify(config: Config, subject: str, body: str) -> bool:
    try:
        append_line(config.logs / "notifications.log", f"[{iso()}] {subject}\n{body}\n")
    except OSError:
        pass
    command = config.get("MEMEX_NOTIFY_CMD").strip()
    if not command:
        return False
    env = dict(os.environ)
    env.update({"MEMEX_SUBJECT": subject, "MEMEX_BODY": body})
    try:
        proc = subprocess.run(["sh", "-c", command], input=body, text=True, env=env, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        eprint(f"memex: could not send the notification ({exc})")
        return False
    if proc.returncode != 0:
        eprint(f"memex: the notification command failed ({proc.returncode}): {proc.stderr.strip()[-200:]}")
        return False
    return True
