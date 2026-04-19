"""User config loader for ~/.config/sticky/config.toml.

Two modes:
- proxy: talk to the hosted `@sticky_bot` via a paired JWT. Default.
- local: user runs their own BotFather bot; we call Bot API directly.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROXY_URL = "http://127.0.0.1:8088"


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "sticky"


def config_path() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    return Path.home() / ".sticky"


@dataclass
class Config:
    mode: str  # "proxy" | "local"
    telegram_user_id: int
    bot_username: str
    # proxy mode
    proxy_url: str | None = None
    # local mode
    bot_token: str | None = None
    # account preference (Telegram-macOS accounts/<id>)
    account_id: str | None = None

    def is_proxy(self) -> bool:
        return self.mode == "proxy"


class ConfigError(RuntimeError):
    pass


def load() -> Config:
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"no config at {path}. Run `sticky init` to set up pairing or a BotFather bot."
        )
    data = tomllib.loads(path.read_text())
    try:
        mode = data["mode"]
        telegram_user_id = int(data["telegram_user_id"])
        bot_username = data["bot_username"]
    except KeyError as exc:
        raise ConfigError(f"missing config field: {exc}") from exc

    if mode == "proxy":
        return Config(
            mode="proxy",
            telegram_user_id=telegram_user_id,
            bot_username=bot_username,
            proxy_url=data.get("proxy_url", DEFAULT_PROXY_URL),
            account_id=data.get("account_id"),
        )
    if mode == "local":
        token = data.get("bot_token")
        if not token:
            raise ConfigError("local mode needs bot_token in config")
        return Config(
            mode="local",
            telegram_user_id=telegram_user_id,
            bot_username=bot_username,
            bot_token=token,
            account_id=data.get("account_id"),
        )
    raise ConfigError(f"unknown mode: {mode}")


def write(cfg: Config) -> Path:
    config_dir().mkdir(parents=True, exist_ok=True)
    lines = [
        f'mode = "{cfg.mode}"',
        f"telegram_user_id = {cfg.telegram_user_id}",
        f'bot_username = "{cfg.bot_username}"',
    ]
    if cfg.mode == "proxy" and cfg.proxy_url:
        lines.append(f'proxy_url = "{cfg.proxy_url}"')
    if cfg.mode == "local" and cfg.bot_token:
        lines.append(f'bot_token = "{cfg.bot_token}"')
    if cfg.account_id:
        lines.append(f'account_id = "{cfg.account_id}"')
    lines.append("")
    path = config_path()
    path.write_text("\n".join(lines))
    path.chmod(0o600)
    return path
