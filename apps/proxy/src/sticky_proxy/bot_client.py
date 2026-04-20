from __future__ import annotations

import httpx

BOT_API_BASE = "https://api.telegram.org"


class BotApiError(RuntimeError):
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body
        super().__init__(f"bot api {status}: {body}")


class BotClient:
    """Minimal Bot API client used by the proxy to forward calls.

    Keeps per-method semantics thin — just HTTP pass-through with JSON response.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._base = f"{BOT_API_BASE}/bot{token}"
        self._file_base = f"{BOT_API_BASE}/file/bot{token}"
        self._client = httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        *,
        data: dict | None = None,
        files: dict | None = None,
    ) -> dict:
        url = f"{self._base}/{method}"
        resp = await self._client.post(url, data=data, files=files)
        payload: dict
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text}
        if not resp.is_success or not payload.get("ok", False):
            raise BotApiError(resp.status_code, payload)
        return payload["result"]

    async def download(self, file_path: str) -> bytes:
        """Fetch a file by its Bot-API-issued `file_path` (from getFile)."""
        url = f"{self._file_base}/{file_path}"
        resp = await self._client.get(url)
        if not resp.is_success:
            raise BotApiError(resp.status_code, {"description": "download failed"})
        return resp.content
