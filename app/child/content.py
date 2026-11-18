"""Montar e enviar posts: texto, mídias (foto, vídeo, álbum) e botões de link."""

import html
import json
import random
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo

BUTTONS_ONLY_TEXT = "⠀"  # invisível: mensagem que só leva os botões embaixo de um álbum
TAG_RE = re.compile(r"<[^>]+>")
CUSTOM_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)
URL_RE = re.compile(r"^(https?://|tg://)\S+$", re.I)


def plain_len(text_html: str) -> int:
    return len(html.unescape(TAG_RE.sub("", text_html or "")))


def strip_custom_emoji(text_html: str) -> str:
    return CUSTOM_EMOJI_RE.sub(r"\1", text_html or "")


# ---------- botões ----------

def normalize_url(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith("@") and len(raw) > 1:
        raw = "https://t.me/" + raw[1:]
    elif re.match(r"^(t\.me|telegram\.me)/", raw, re.I) or re.match(r"^[\w-]+\.[a-z]{2,}(/|$)", raw, re.I):
        raw = "https://" + raw
    return raw if URL_RE.match(raw) else None


def parse_buttons(text: str) -> tuple[list[list[tuple[str, str]]], list[str]]:
    """'Texto - link' por linha; vários na mesma linha separados por | ou &&."""
    rows, errors = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for part in re.split(r"\s*(?:\||&&)\s*", line):
            if " - " not in part:
                errors.append(part)
                continue
            label, link = part.rsplit(" - ", 1)
            link = normalize_url(link)
            if not label.strip() or not link:
                errors.append(part)
                continue
            row.append((label.strip()[:64], link))
        if row:
            rows.append(row[:4])
    return rows[:10], errors


def buttons_text(rows: list[list[tuple[str, str]]]) -> str:
    return "\n".join(" | ".join(f"{t} - {u}" for t, u in row) for row in rows)


def buttons_markup(stored: str) -> InlineKeyboardMarkup | None:
    rows, _ = parse_buttons(stored)
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, url=u) for t, u in row] for row in rows])


# ---------- mídias ----------

def load_media(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
        return [m for m in data if m.get("t") in ("photo", "video") and m.get("f")]
    except ValueError:
        return []


def media_from_message(message) -> dict | None:
    if message.photo:
        return {"t": "photo", "f": message.photo[-1].file_id}
    if message.video:
        return {"t": "video", "f": message.video.file_id}
    return None


def media_summary(media: list[dict]) -> str:
    if not media:
        return "sem mídia"
    if len(media) > 1:
        return f"álbum com {len(media)}"
    return "foto" if media[0]["t"] == "photo" else "vídeo"


def media_icon(media: list[dict]) -> str:
    if len(media) > 1:
        return "📚"
    if media and media[0]["t"] == "video":
        return "🎬"
    return "🖼"


def random_text(raw: str) -> str:
    options = [t.strip() for t in re.split(r"\n-{3,}\n", raw or "") if t.strip()]
    return random.choice(options) if options else ""


# ---------- enviar ----------

async def send_post(bot: Bot, chat_id: int, text_html: str, media: list[dict],
                    markup: InlineKeyboardMarkup | None, caption_above: bool = False) -> list[int]:
    """Envia o post e devolve os ids das mensagens. Se o Telegram recusar o emoji premium, manda o comum."""
    try:
        return await _send(bot, chat_id, text_html, media, markup, caption_above)
    except TelegramBadRequest:
        plain = strip_custom_emoji(text_html)
        if plain == text_html:
            raise
        return await _send(bot, chat_id, plain, media, markup, caption_above)


async def _send(bot: Bot, chat_id: int, text: str, media: list[dict], markup, above: bool) -> list[int]:
    text = text or ""
    if not media:
        if not text.strip():
            raise ValueError("post vazio")
        m = await bot.send_message(chat_id, text, reply_markup=markup)
        return [m.message_id]
    long_text = plain_len(text) > 1024  # legenda grande demais: mídia primeiro, texto depois
    caption = None if long_text or not text.strip() else text
    ids: list[int] = []
    if len(media) == 1:
        m0 = media[0]
        send = bot.send_photo if m0["t"] == "photo" else bot.send_video
        msg = await send(chat_id, m0["f"], caption=caption, show_caption_above_media=above if caption else None,
                         reply_markup=None if long_text else markup)
        ids.append(msg.message_id)
    else:
        items = []
        for n, m in enumerate(media[:10]):
            c = caption if n == 0 else None
            kw = {"caption": c, "show_caption_above_media": above if c else None}
            items.append(InputMediaPhoto(media=m["f"], **kw) if m["t"] == "photo" else InputMediaVideo(media=m["f"], **kw))
        msgs = await bot.send_media_group(chat_id, items)
        ids += [x.message_id for x in msgs]
    if long_text:
        m = await bot.send_message(chat_id, text, reply_markup=markup)
        ids.append(m.message_id)
    elif len(media) > 1 and markup is not None:
        m = await bot.send_message(chat_id, BUTTONS_ONLY_TEXT, reply_markup=markup)  # álbum não aceita botões
        ids.append(m.message_id)
    return ids
