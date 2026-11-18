"""Botões, formato dos callbacks e helpers de tela."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


class MP(CallbackData, prefix="mp"):
    """Botões do bot mãe."""
    s: str  # tela
    a: str = ""  # ação
    i: int = 0


class CP(CallbackData, prefix="cp"):
    """Botões do painel do bot filho."""
    s: str
    a: str = ""
    i: int = 0


Screen = tuple[str, InlineKeyboardMarkup]


def kb(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def mbtn(text: str, s: str, a: str = "", i: int = 0, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=MP(s=s, a=a, i=i).pack(), style=style)


def cbtn(text: str, s: str, a: str = "", i: int = 0, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=CP(s=s, a=a, i=i).pack(), style=style)


def url(text: str, link: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=link)


async def show(cb: CallbackQuery, screen: Screen) -> None:
    """Troca a tela atual (edita a mensagem do painel)."""
    text, markup = screen
    try:
        await cb.message.edit_text(text, reply_markup=markup, link_preview_options={"is_disabled": True})
    except TelegramBadRequest as e:
        if "message is not modified" not in e.message:
            await cb.message.answer(text, reply_markup=markup, link_preview_options={"is_disabled": True})


async def send(message: Message, screen: Screen) -> Message:
    text, markup = screen
    return await message.answer(text, reply_markup=markup, link_preview_options={"is_disabled": True})
