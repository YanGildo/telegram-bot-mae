"""Liga, desliga e mantém rodando os bots filhos.

Todos rodam no mesmo programa: cada filho tem sua própria fila de atualizações (getUpdates)
e todos usam o mesmo conjunto de handlers do filho (app/child). Dá para ligar um bot novo
sem reiniciar nada.
"""

import asyncio
import logging
from typing import Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramBadRequest, TelegramConflictError, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError)
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from sqlalchemy import select

from .config import Settings
from .db import ChildBot, Database
from .security import TokenBox

logger = logging.getLogger(__name__)

CHILD_COMMANDS = [
    BotCommand(command="start", description="Abrir o painel"),
    BotCommand(command="ajuda", description="Como usar"),
    BotCommand(command="id", description="Mostrar IDs"),
]


class BotManager:
    def __init__(self, db: Database, settings: Settings, child_dp: Dispatcher, mother: Bot,
                 session_factory: Callable[[], BaseSession] | None = None, poll_timeout: int = 25):
        self.db, self.settings, self.dp, self.mother = db, settings, child_dp, mother
        self.box = TokenBox(settings.secret_key.get_secret_value())
        self.session_factory = session_factory
        self.poll_timeout = poll_timeout
        self.bots: dict[int, Bot] = {}  # child_id → Bot
        self.tasks: dict[int, asyncio.Task] = {}
        self.mother_username = ""
        self.allowed_updates = child_dp.resolve_used_update_types()

    # ---------- criar Bot ----------

    def make_bot(self, token: str) -> Bot:
        kwargs = {"session": self.session_factory()} if self.session_factory else {}
        return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), **kwargs)

    def is_running(self, child_id: int) -> bool:
        t = self.tasks.get(child_id)
        return t is not None and not t.done()

    # ---------- ligar / desligar ----------

    async def start_all(self) -> int:
        async with self.db.session() as s:
            rows = list(await s.scalars(select(ChildBot).where(ChildBot.status == "active")))
        n = 0
        for row in rows:
            n += await self.start(row)
        logger.info("%s bot(s) filho(s) ligado(s)", n)
        return n

    async def start(self, row: ChildBot) -> bool:
        if self.is_running(row.id):
            return True
        token = self.box.open(row.token_enc)
        if token is None:
            await self._mark(row.id, "invalid", "não consegui abrir o token (SECRET_KEY mudou?)")
            return False
        bot = self.make_bot(token)
        try:
            await bot.delete_webhook(drop_pending_updates=False)  # polling não funciona com webhook ligado
            await bot.set_my_commands(CHILD_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        except TelegramUnauthorizedError:
            await bot.session.close()
            await self._mark(row.id, "invalid", "token revogado ou inválido")
            await self._warn_owner(row, "⚠️ O token do seu bot @{u} não funciona mais (foi revogado?). "
                                        "Abra 🤖 Meus bots → @{u} → 🔄 Trocar token.")
            return False
        except (TelegramNetworkError, TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning("Bot filho %s: aviso ao ligar: %s", row.username, e)
        self.bots[row.id] = bot
        self.tasks[row.id] = asyncio.create_task(self._poll(row.id, bot), name=f"filho-{row.username}")
        await self._mark(row.id, "active", None)
        logger.info("Bot filho @%s ligado", row.username)
        return True

    async def stop(self, child_id: int) -> None:
        task = self.tasks.pop(child_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        bot = self.bots.pop(child_id, None)
        if bot is not None:
            await bot.session.close()

    async def stop_all(self) -> None:
        for child_id in list(self.tasks):
            await self.stop(child_id)

    async def restart(self, row: ChildBot) -> bool:
        await self.stop(row.id)
        return await self.start(row)

    def bot_for(self, child_id: int) -> Bot | None:
        return self.bots.get(child_id)

    # ---------- fila de atualizações ----------

    async def _poll(self, child_id: int, bot: Bot) -> None:
        offset = None
        conflict_warned = False
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=self.poll_timeout,
                                                allowed_updates=self.allowed_updates,
                                                request_timeout=self.poll_timeout + 10)
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                row = await self._mark(child_id, "invalid", "token revogado ou inválido")
                if row:
                    await self._warn_owner(row, "⚠️ O token do seu bot @{u} parou de funcionar (foi revogado?). "
                                                "Abra 🤖 Meus bots → @{u} → 🔄 Trocar token.")
                self.tasks.pop(child_id, None)
                self.bots.pop(child_id, None)
                await bot.session.close()
                return
            except TelegramConflictError:
                if not conflict_warned:
                    conflict_warned = True
                    row = await self._mark(child_id, "active", "outro programa está usando este token")
                    if row:
                        await self._warn_owner(row, "⚠️ Outro programa está usando o token do @{u}. "
                                                    "Enquanto isso, ele pode não responder. "
                                                    "Desligue o outro programa ou troque o token.")
                await asyncio.sleep(10)
                continue
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except (TelegramNetworkError, asyncio.TimeoutError):
                await asyncio.sleep(3)
                continue
            except Exception:
                logger.exception("Erro lendo atualizações do bot filho %s", child_id)
                await asyncio.sleep(5)
                continue
            if conflict_warned:
                conflict_warned = False
                await self._mark(child_id, "active", None)
            for update in updates:
                offset = update.update_id + 1
                try:
                    await self.dp.feed_update(bot, update)
                except Exception:
                    logger.exception("Erro tratando atualização %s do bot filho %s", update.update_id, child_id)

    # ---------- auxiliares ----------

    async def _mark(self, child_id: int, status: str, error: str | None) -> ChildBot | None:
        async with self.db.session() as s:
            row = await s.get(ChildBot, child_id)
            if row is None:
                return None
            row.status, row.last_error = status, error
            await s.commit()
            return row

    async def _warn_owner(self, row: ChildBot, text: str) -> None:
        try:
            await self.mother.send_message(row.owner_id, text.format(u=row.username))
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as e:
            logger.info("Não avisei o dono do @%s: %s", row.username, e)
