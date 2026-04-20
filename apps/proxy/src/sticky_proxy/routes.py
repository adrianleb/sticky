from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .auth import decode_token, encode_token
from .bot_client import BotApiError, BotClient
from .config import Settings, get_settings
from .pairing import PairStore

logger = logging.getLogger(__name__)

router = APIRouter()


class PairRequest(BaseModel):
    code: str


class PairResponse(BaseModel):
    token: str
    telegram_user_id: int
    bot_username: str


def get_pair_store(request: Request) -> PairStore:
    return request.app.state.pair_store


def get_bot_client(request: Request) -> BotClient:
    return request.app.state.bot_client


async def authenticated_user(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> int:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth[7:].strip()
    try:
        return decode_token(settings.jwt_secret, token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc


@router.post("/pair", response_model=PairResponse)
async def pair(
    payload: PairRequest,
    store: PairStore = Depends(get_pair_store),
    settings: Settings = Depends(get_settings),
) -> PairResponse:
    user_id = await store.consume(payload.code.strip())
    if user_id is None:
        raise HTTPException(status_code=400, detail="code invalid or expired")
    token = encode_token(settings.jwt_secret, user_id)
    return PairResponse(
        token=token,
        telegram_user_id=user_id,
        bot_username=settings.bot_username,
    )


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ─── Bot API proxy ──────────────────────────────────────────────────────────


async def _forward(
    bot_client: BotClient,
    method: str,
    data: dict,
    files: dict | None = None,
) -> JSONResponse:
    try:
        result = await bot_client.call(method, data=data, files=files)
    except BotApiError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": exc.body})
    return JSONResponse(content={"ok": True, "result": result})


@router.post("/bot/upload_sticker_file")
async def upload_sticker_file(
    sticker: UploadFile,
    sticker_format: str = "static",
    user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    content = await sticker.read()
    filename = sticker.filename or "sticker.png"
    files = {"sticker": (filename, content, sticker.content_type or "image/png")}
    data = {"user_id": str(user_id), "sticker_format": sticker_format}
    return await _forward(bot_client, "uploadStickerFile", data=data, files=files)


class CreateStickerSetRequest(BaseModel):
    name: str
    title: str
    stickers: list[dict]
    sticker_type: str | None = None


@router.post("/bot/create_new_sticker_set")
async def create_new_sticker_set(
    payload: CreateStickerSetRequest,
    user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    import json

    data = {
        "user_id": str(user_id),
        "name": payload.name,
        "title": payload.title,
        "stickers": json.dumps(payload.stickers),
    }
    if payload.sticker_type:
        data["sticker_type"] = payload.sticker_type
    return await _forward(bot_client, "createNewStickerSet", data=data)


class AddStickerRequest(BaseModel):
    name: str
    sticker: dict


@router.post("/bot/add_sticker_to_set")
async def add_sticker_to_set(
    payload: AddStickerRequest,
    user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    import json

    data = {
        "user_id": str(user_id),
        "name": payload.name,
        "sticker": json.dumps(payload.sticker),
    }
    return await _forward(bot_client, "addStickerToSet", data=data)


class StickerRefRequest(BaseModel):
    sticker: str


@router.post("/bot/delete_sticker_from_set")
async def delete_sticker_from_set(
    payload: StickerRefRequest,
    _user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    return await _forward(bot_client, "deleteStickerFromSet", data={"sticker": payload.sticker})


class SetPositionRequest(BaseModel):
    sticker: str
    position: int


@router.post("/bot/set_sticker_position_in_set")
async def set_sticker_position(
    payload: SetPositionRequest,
    _user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    data = {"sticker": payload.sticker, "position": str(payload.position)}
    return await _forward(bot_client, "setStickerPositionInSet", data=data)


class SendMessageRequest(BaseModel):
    text: str
    parse_mode: str | None = None
    disable_web_page_preview: bool | None = None


@router.post("/bot/send_message")
async def send_message(
    payload: SendMessageRequest,
    user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    """DM the paired user. `chat_id` is always forced to the authenticated user."""
    data: dict[str, str] = {"chat_id": str(user_id), "text": payload.text}
    if payload.parse_mode:
        data["parse_mode"] = payload.parse_mode
    if payload.disable_web_page_preview is not None:
        data["disable_web_page_preview"] = "true" if payload.disable_web_page_preview else "false"
    return await _forward(bot_client, "sendMessage", data=data)


class GetStickerSetRequest(BaseModel):
    name: str


@router.post("/bot/get_sticker_set")
async def get_sticker_set(
    payload: GetStickerSetRequest,
    _user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    return await _forward(bot_client, "getStickerSet", data={"name": payload.name})


class GetFileRequest(BaseModel):
    file_id: str


@router.post("/bot/get_file")
async def get_file(
    payload: GetFileRequest,
    _user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> JSONResponse:
    return await _forward(bot_client, "getFile", data={"file_id": payload.file_id})


class DownloadFileRequest(BaseModel):
    file_path: str


@router.post("/bot/download_file")
async def download_file(
    payload: DownloadFileRequest,
    _user_id: int = Depends(authenticated_user),
    bot_client: BotClient = Depends(get_bot_client),
) -> Response:
    """Stream a sticker body through the proxy.

    The `file_path` comes from a prior `getFile` call; the proxy owns the bot
    token (needed to address `/file/bot<token>/...`) so the client can't fetch
    directly in proxy mode.
    """
    try:
        body = await bot_client.download(payload.file_path)
    except BotApiError as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": exc.body})
    return Response(content=body, media_type="application/octet-stream")
