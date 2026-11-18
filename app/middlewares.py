"""Middlewares que rodam antes dos handlers."""

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update
from sqlalchemy.exc import IntegrityError

from . import repo
from .db import Database, ProcessedUpdate, utcnow

logger = logging.getLogger(__name__)


class DedupeMiddleware(BaseMiddleware):
    """Descarta atualizações que o Telegram entregar mais de uma vez (por bot)."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(self, handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
                       event: Update, data: dict[str, Any]) -> Any:
        bot = data["bot"]
        async with self.db.session() as s:
            s.add(ProcessedUpdate(bot_id=bot.id, update_id=event.update_id, received_at=utcnow()))
            try:
                await s.commit()
            except IntegrityError:
                logger.warning("bot=%s update_id=%s repetido, ignorado", bot.id, event.update_id)
                return None
        return await handler(event, data)


class ChildMiddleware(BaseMiddleware):
    """Descobre qual bot filho recebeu a atualização e entrega como `child` aos handlers."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(self, handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
                       event: Update, data: dict[str, Any]) -> Any:
        child = await repo.bot_by_telegram_id(self.db, data["bot"].id)
        if child is None or child.status == "stopped":
            return None
        data["child"] = child
        return await handler(event, data)
