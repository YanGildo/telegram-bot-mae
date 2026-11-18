"""Liga o bot mãe e todos os bots filhos.  Uso: python -m app"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from .child import handlers as child_handlers
from .config import Settings, load_settings
from .db import Database
from .manager import BotManager
from .middlewares import ChildMiddleware, DedupeMiddleware
from .mother import handlers as mother_handlers

logger = logging.getLogger(__name__)

MOTHER_COMMANDS = [
    BotCommand(command="start", description="Início"),
    BotCommand(command="id", description="Mostrar seu ID"),
    BotCommand(command="cancelar", description="Cancelar o que está fazendo"),
]


def build_child_dispatcher(db: Database, settings: Settings) -> Dispatcher:
    dp = Dispatcher(db=db, settings=settings)
    dp.update.outer_middleware(DedupeMiddleware(db))
    dp.update.outer_middleware(ChildMiddleware(db))
    dp.include_router(child_handlers.router)
    return dp


def build_mother_dispatcher(db: Database, settings: Settings, manager: BotManager) -> Dispatcher:
    dp = Dispatcher(db=db, settings=settings, manager=manager)
    dp.update.outer_middleware(DedupeMiddleware(db))
    dp.include_router(mother_handlers.router)
    return dp


async def prune_loop(db: Database) -> None:
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await db.prune_updates()
        except Exception:
            logger.exception("Erro limpando atualizações antigas")


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    db = Database(settings.database_url)
    await db.init()
    mother = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await mother.get_me()

    child_dp = build_child_dispatcher(db, settings)
    manager = BotManager(db, settings, child_dp, mother)
    manager.mother_username = me.username or ""
    child_dp["manager"] = manager
    mother_dp = build_mother_dispatcher(db, settings, manager)

    await mother.set_my_commands(MOTHER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await manager.start_all()
    pruner = asyncio.create_task(prune_loop(db))

    logger.info("Bot mãe @%s no ar. Ctrl+C para parar.", me.username)
    if not settings.owners:
        logger.warning("ADMIN_IDS vazio: mande /id para o bot mãe e coloque seu número no .env")
    try:
        await mother_dp.start_polling(mother, allowed_updates=mother_dp.resolve_used_update_types())
    finally:
        pruner.cancel()
        await manager.stop_all()
        await mother.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot parado.")
