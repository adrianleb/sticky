"""Unified Bot API client.

Talks to the sticky proxy (proxy mode) or directly to api.telegram.org
(local mode, with the user's own BotFather-issued token).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config

BOT_API_BASE = "https://api.telegram.org"


class BotApiError(RuntimeError):
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body
        super().__init__(f"bot api {status}: {body}")


@dataclass
class UploadedSticker:
    file_id: str
    file_unique_id: str


class BotClient:
    """Send Bot API calls through proxy or directly.

    In proxy mode the proxy injects `user_id` from the JWT — callers don't
    pass it here. In local mode the client adds `user_id` itself using
    `config.telegram_user_id`.
    """

    def __init__(
        self,
        config: Config,
        *,
        jwt_token: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self._config = config
        self._jwt = jwt_token
        self._client = httpx.AsyncClient(timeout=timeout)
        # Bot API rate limits: 1 createNewStickerSet per second; editing a given
        # set should space ~10s between calls. A single global semaphore + sleep
        # between calls on the same set is enough for one-user scale.
        self._api_gate = asyncio.Semaphore(1)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ─── Low-level transport ────────────────────────────────────────────

    def _proxy_url(self, path: str) -> str:
        base = self._config.proxy_url or "http://127.0.0.1:8088"
        return base.rstrip("/") + path

    def _direct_url(self, method: str) -> str:
        return f"{BOT_API_BASE}/bot{self._config.bot_token}/{method}"

    def _auth_headers(self) -> dict[str, str]:
        if self._config.is_proxy():
            if not self._jwt:
                raise RuntimeError("proxy mode requires a paired JWT")
            return {"Authorization": f"Bearer {self._jwt}"}
        return {}

    async def _post_json(self, path_or_method: str, payload: dict) -> dict:
        async with self._api_gate:
            if self._config.is_proxy():
                url = self._proxy_url(path_or_method)
                resp = await self._client.post(
                    url, json=payload, headers=self._auth_headers()
                )
                return self._unwrap_proxy(resp)
            url = self._direct_url(path_or_method)
            data = {k: (json.dumps(v) if isinstance(v, dict | list) else str(v))
                    for k, v in payload.items()}
            resp = await self._client.post(url, data=data)
            return self._unwrap_direct(resp)

    async def _post_multipart(
        self,
        path_or_method: str,
        *,
        data: dict,
        files: dict,
    ) -> dict:
        async with self._api_gate:
            if self._config.is_proxy():
                url = self._proxy_url(path_or_method)
                resp = await self._client.post(
                    url, data=data, files=files, headers=self._auth_headers()
                )
                return self._unwrap_proxy(resp)
            url = self._direct_url(path_or_method)
            resp = await self._client.post(url, data=data, files=files)
            return self._unwrap_direct(resp)

    def _unwrap_proxy(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        if not resp.is_success or not body.get("ok", False):
            raise BotApiError(resp.status_code, body)
        return body.get("result", {})

    def _unwrap_direct(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        if not resp.is_success or not body.get("ok", False):
            raise BotApiError(resp.status_code, body)
        return body["result"]

    # ─── High-level Bot API calls ───────────────────────────────────────

    async def upload_sticker_file(
        self,
        png_path: Path,
        sticker_format: str = "static",
    ) -> UploadedSticker:
        with png_path.open("rb") as fh:
            content = fh.read()
        filename = png_path.name
        if self._config.is_proxy():
            files = {"sticker": (filename, content, "image/png")}
            data = {"sticker_format": sticker_format}
            result = await self._post_multipart(
                "/bot/upload_sticker_file", data=data, files=files
            )
        else:
            files = {"sticker": (filename, content, "image/png")}
            data = {
                "user_id": str(self._config.telegram_user_id),
                "sticker_format": sticker_format,
            }
            result = await self._post_multipart(
                "uploadStickerFile", data=data, files=files
            )
        return UploadedSticker(
            file_id=result["file_id"],
            file_unique_id=result["file_unique_id"],
        )

    async def create_new_sticker_set(
        self,
        *,
        name: str,
        title: str,
        stickers: list[dict],
        sticker_type: str | None = None,
    ) -> dict:
        if self._config.is_proxy():
            payload: dict = {"name": name, "title": title, "stickers": stickers}
            if sticker_type:
                payload["sticker_type"] = sticker_type
            return await self._post_json("/bot/create_new_sticker_set", payload)
        data: dict = {
            "user_id": self._config.telegram_user_id,
            "name": name,
            "title": title,
            "stickers": stickers,
        }
        if sticker_type:
            data["sticker_type"] = sticker_type
        return await self._post_json("createNewStickerSet", data)

    async def add_sticker_to_set(self, *, name: str, sticker: dict) -> dict:
        if self._config.is_proxy():
            return await self._post_json(
                "/bot/add_sticker_to_set", {"name": name, "sticker": sticker}
            )
        return await self._post_json(
            "addStickerToSet",
            {
                "user_id": self._config.telegram_user_id,
                "name": name,
                "sticker": sticker,
            },
        )

    async def delete_sticker_from_set(self, sticker_file_id: str) -> dict:
        path = "/bot/delete_sticker_from_set" if self._config.is_proxy() else "deleteStickerFromSet"
        return await self._post_json(path, {"sticker": sticker_file_id})

    async def set_sticker_position_in_set(
        self, sticker_file_id: str, position: int
    ) -> dict:
        path = (
            "/bot/set_sticker_position_in_set"
            if self._config.is_proxy()
            else "setStickerPositionInSet"
        )
        return await self._post_json(path, {"sticker": sticker_file_id, "position": position})

    async def get_sticker_set(self, name: str) -> dict:
        path = "/bot/get_sticker_set" if self._config.is_proxy() else "getStickerSet"
        return await self._post_json(path, {"name": name})

    async def get_file(self, bot_file_id: str) -> dict:
        path = "/bot/get_file" if self._config.is_proxy() else "getFile"
        return await self._post_json(path, {"file_id": bot_file_id})

    async def download_file_bytes(self, file_path: str) -> bytes:
        """Fetch the raw bytes of a file returned by `get_file`.

        Proxy mode routes through the proxy (which holds the bot token); local
        mode hits the Telegram file CDN directly.
        """
        async with self._api_gate:
            if self._config.is_proxy():
                url = self._proxy_url("/bot/download_file")
                resp = await self._client.post(
                    url, json={"file_path": file_path}, headers=self._auth_headers()
                )
                if not resp.is_success:
                    try:
                        body = resp.json()
                    except Exception:
                        body = {"raw": resp.text}
                    raise BotApiError(resp.status_code, body)
                return resp.content
            url = f"{BOT_API_BASE}/file/bot{self._config.bot_token}/{file_path}"
            resp = await self._client.get(url)
            if not resp.is_success:
                raise BotApiError(resp.status_code, {"description": "download failed"})
            return resp.content

    async def send_message_to_self(self, text: str, *, parse_mode: str | None = None) -> dict:
        if self._config.is_proxy():
            payload: dict = {"text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            return await self._post_json("/bot/send_message", payload)
        data: dict = {"chat_id": self._config.telegram_user_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        return await self._post_json("sendMessage", data)
