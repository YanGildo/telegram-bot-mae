"""Canal de registros do bot filho: punições, pedidos de entrada e avisos do acervo."""

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User as TgUser

from ..db import ChildBot, Database

logger = logging.getLogger(__name__)


class LG(CallbackData, prefix="lg"):
    """Botões das mensagens do canal de registros."""
    a: str  # unmute | unban | reset (zerar advertências)
    c: int  # chat_id
    u: int  # user_id


def who(user: TgUser | None) -> str:
    if user is None:
        return "alguém"
    name = escape(user.full_name or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>' + (f" (@{escape(user.username)})" if user.username else "") \
        + f" · <code>{user.id}</code>"


def action_markup(action: str, chat_id: int, user_id: int) -> InlineKeyboardMarkup | None:
    if action == "mute":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔊 Desmutar", callback_data=LG(a="unmute", c=chat_id, u=user_id).pack()),
            InlineKeyboardButton(text="🚷 Banir", callback_data=LG(a="ban", c=chat_id, u=user_id).pack())]])
    if action == "ban":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="♻️ Desbanir", callback_data=LG(a="unban", c=chat_id, u=user_id).pack())]])
    if action == "warn":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🧹 Zerar advertências", callback_data=LG(a="reset", c=chat_id, u=user_id).pack())]])
    return None


async def send_log(bot: Bot, db: Database, child: ChildBot, text: str, markup: InlineKeyboardMarkup | None = None,
                   owner_fallback: bool = False) -> bool:
    """Manda no canal de registros. Sem canal escolhido, só manda no privado do dono se owner_fallback."""
    async with db.session() as s:
        row = await s.get(ChildBot, child.id)
        log_chat = row.log_chat_id if row else None
    targets = [log_chat] if log_chat else []
    if owner_fallback:
        targets.append(child.owner_id)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text, reply_markup=markup, disable_notification=True,
                                   link_preview_options={"is_disabled": True})
            return True
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.info("Registro do @%s não enviado em %s: %s", child.username, chat_id, e)
    return False
