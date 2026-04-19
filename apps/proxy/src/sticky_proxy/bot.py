from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

from .pairing import PairStore

logger = logging.getLogger(__name__)

START_TEXT = (
    "👋 Hi! I'm sticky.\n\n"
    "I help you turn your Telegram sticker usage into dynamic packs "
    "(\"All-time Top 30\", \"This Month\", …) that install natively on every device.\n\n"
    "To get started: install the sticky CLI on your Mac, then send /pair here."
)

PAIR_TEXT = (
    "Your pairing code is:\n\n"
    "<code>{code}</code>\n\n"
    "Paste it into your terminal:\n"
    "<code>sticky pair {code}</code>\n\n"
    "Expires in 5 minutes."
)


def build_dispatcher(pair_store: PairStore) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(START_TEXT)

    @dp.message(F.text.regexp(r"^/pair(\s|$)"))
    async def on_pair(message: Message) -> None:
        if message.from_user is None:
            return
        code = await pair_store.create(message.from_user.id)
        await message.answer(PAIR_TEXT.format(code=code), parse_mode="HTML")

    return dp


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    logger.info("starting aiogram polling")
    await dp.start_polling(bot, handle_signals=False)
