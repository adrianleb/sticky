from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI

from .bot import build_dispatcher, run_polling
from .bot_client import BotClient
from .config import get_settings
from .pairing import PairStore
from .routes import router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sticky_proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pair_store = PairStore(ttl_sec=settings.pair_code_ttl_sec)
    bot_client = BotClient(settings.bot_token)
    aio_bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dispatcher = build_dispatcher(pair_store)

    app.state.pair_store = pair_store
    app.state.bot_client = bot_client
    app.state.aiogram_bot = aio_bot

    polling_task = asyncio.create_task(run_polling(aio_bot, dispatcher))
    logger.info("sticky-proxy ready on %s:%s (bot @%s)",
                settings.host, settings.port, settings.bot_username)
    try:
        yield
    finally:
        polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling_task
        await aio_bot.session.close()
        await bot_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="sticky-proxy", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
