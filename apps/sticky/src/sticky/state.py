"""Agent-local state: peer-hashing salt, persisted as a flat JSON file.

Everything else (last-sync timestamp, account id) lives in config.toml or
the local SQLite DB.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR = Path.home() / ".sticky"
STATE_PATH = APP_DIR / "state.json"


@dataclass
class AgentState:
    peer_salt_hex: str = field(default_factory=lambda: secrets.token_hex(32))

    @property
    def peer_salt(self) -> bytes:
        return bytes.fromhex(self.peer_salt_hex)


def load_state() -> AgentState:
    if not STATE_PATH.exists():
        state = AgentState()
        save_state(state)
        return state
    data = json.loads(STATE_PATH.read_text())
    return AgentState(
        peer_salt_hex=data.get("peer_salt_hex") or secrets.token_hex(32),
    )


def save_state(state: AgentState) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, STATE_PATH)


def reset_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()
