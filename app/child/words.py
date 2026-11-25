"""Palavras proibidas: listas por grupo, com punição. Também conta mensagens para as programações."""

import logging
import re
import time
import unicodedata
from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatPermissions, Message
from sqlalchemy import select

from .. import repo
from ..db import ChildBot, ChildChat, Database, PendingDelete, WarnCount, WordFilter, utcnow
from ..ui import CP, Screen, cbtn, kb, show
from .flow import ask
from .logs import LG, action_markup, send_log, who
from .scheduling import interval_text, on_group_message

logger = logging.getLogger(__name__)
router = Router(name="palavras")

ACTIONS = {"off": "❌ Desativado", "warn": "❕ Advertência", "kick": "❗ Kick", "mute": "🔇 Silenciar", "ban": "🚷 Ban"}
DURATIONS = [0, 60, 360, 1440, 4320, 10080, 43200]
NOTICE_SECONDS = 60  # avisos do bot no grupo somem depois de 1 minuto
_admin_cache: dict[tuple[int, int], tuple[float, set[int]]] = {}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def word_list(f: WordFilter) -> list[str]:
    return [w for w in (f.words or "").splitlines() if w.strip()]


def find_banned(text: str, f: WordFilter) -> str | None:
    norm = normalize(text)
    for w in word_list(f):
        nw = normalize(w.strip())
        if not nw:
            continue
        if f.match == "exact":
            if re.search(r"(?<!\w)" + re.escape(nw) + r"(?!\w)", norm):
                return w
        elif nw in norm:
            return w
    return None


def penalty_text(f: WordFilter) -> str:
    if f.action == "off":
        return "Desativado"
    if f.action == "warn":
        then = {"mute": "silenciar", "kick": "expulsar", "ban": "banir"}[f.warn_then]
        return f"Advertência (com {f.warn_limit}: {then}" + (f" por {interval_text(f.duration_min)}" if f.duration_min and f.warn_then != "kick" else "") + ")"
    base = {"kick": "Kick (expulsa, pode voltar)", "mute": "Silenciar", "ban": "Ban"}[f.action]
    if f.action in ("mute", "ban"):
        base += f" por {interval_text(f.duration_min)}" if f.duration_min else " para sempre"
    return base


async def get_filter(db: Database, child_id: int, chat_row_id: int) -> WordFilter:
    async with db.session() as s:
        f = await s.scalar(select(WordFilter).where(WordFilter.child_id == child_id, WordFilter.chat_row_id == chat_row_id))
        if f is None:
            f = WordFilter(child_id=child_id, chat_row_id=chat_row_id)
            s.add(f)
            await s.commit()
        return f


# ---------- telas ----------

async def screen_groups(child: ChildBot, db: Database) -> Screen:
    groups = [c for c in await repo.child_chats(db, child.id) if c.type != ChatType.CHANNEL]
    lines = ["🚫 <b>Palavras proibidas</b>\n",
             "Escolha o grupo. Cada grupo tem a própria lista e a própria punição, sem limite de palavras.\n",
             "O bot precisa ser administrador com <b>Apagar mensagens</b> e <b>Banir usuários</b>. "
             "Admins do grupo e do bot nunca são punidos.\n"]
    rows = []
    async with db.session() as s:
        for c in groups:
            f = await s.scalar(select(WordFilter).where(WordFilter.child_id == child.id, WordFilter.chat_row_id == c.id))
            on = f is not None and f.action != "off" and word_list(f)
            n = len(word_list(f)) if f else 0
            lines.append(f"{'✅' if on else '❌'} {escape(c.title)} · {n} palavra(s)")
            rows.append([cbtn(f"{'✅' if on else '❌'} {c.title[:40]}", "wf", "", c.id)])
    if not groups:
        lines.append("<i>O bot ainda não está em nenhum grupo.</i>")
    return "\n".join(lines), kb(*rows, [cbtn("« Painel", "home")])


async def screen_filter(child: ChildBot, db: Database, chat: ChildChat) -> Screen:
    f = await get_filter(db, child.id, chat.id)
    i = chat.id
    text = (f"🔤 <b>Palavras proibidas</b> · {escape(chat.title)}\n\n"
            "Coloque uma punição para quem usar as palavras da lista.\n\n"
            f"Penalidade: <b>{penalty_text(f)}</b>\n"
            f"Apagar a mensagem: {'Sim ✅' if f.delete else 'Não ❌'}\n"
            f"Busca: {'palavra exata' if f.match == 'exact' else 'contém o termo'} · ignora acentos e maiúsculas\n"
            f"Palavras na lista: {len(word_list(f))}\n\n"
            "<b>O que cada botão faz</b>\n"
            "• ❌ ❕ ❗ 🔇 🚷: escolhe a punição.\n"
            "• 🗑 Apagar mensagens: apaga ou não a mensagem com a palavra.\n"
            "• ⏱ Duração: por quanto tempo vale o silenciar/ban.\n"
            "• 🔎 Busca: <i>contém</i> pega 'promo' dentro de 'promoção'; <i>exata</i> só a palavra inteira.\n"
            "• ➕ / ➖: adiciona ou tira palavras (várias de uma vez, uma por linha ou separadas por vírgula).")

    def act(k):
        return cbtn(("✅ " if f.action == k else "") + ACTIONS[k], "wf", "act", i, j=list(ACTIONS).index(k))
    rows = [[act("off"), act("warn"), act("kick")], [act("mute"), act("ban")],
            [cbtn(f"🗑 Apagar mensagens {'✅' if f.delete else '❌'}", "wf", "del", i)],
            [cbtn("⏱ Definir a duração", "wf", "dur", i)]]
    if f.action == "warn":
        rows.append([cbtn(f"⚠️ Advertências até punir: {f.warn_limit}", "wf", "wl", i),
                     cbtn("Depois: " + {"mute": "🔇", "kick": "❗", "ban": "🚷"}[f.warn_then], "wf", "wt", i)])
    rows += [[cbtn("🔎 Busca: " + ("exata" if f.match == "exact" else "contém"), "wf", "match", i)],
             [cbtn("➕ Adicionar", "wf", "add", i), cbtn("➖ Remover", "wf", "rem", i)],
             [cbtn(f"🔤 Lista ({len(word_list(f))})", "wf", "list", i)],
             [cbtn("« Grupos", "words")]]
    return text, kb(*rows)


def screen_duration(f: WordFilter, i: int) -> Screen:
    text = ("⏱ <b>Duração da punição</b>\n\nVale para silenciar e ban (também quando a advertência chega no limite). "
            "Kick não tem duração: a pessoa sai e pode voltar.")
    btns = [cbtn(("✅ " if f.duration_min == d else "") + (interval_text(d) if d else "Para sempre"), "wf", "setdur", i, j=d)
            for d in DURATIONS]
    return text, kb(btns[:3], btns[3:6], btns[6:], [cbtn("« Voltar", "wf", "", i)])


# ---------- botões ----------

@router.callback_query(CP.filter(F.s.in_({"words", "wf"})))
async def on_words(cb: CallbackQuery, callback_data: CP, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    s_, a, i, j = callback_data.s, callback_data.a, callback_data.i, callback_data.j
    await state.clear()
    if s_ == "words":
        await cb.answer()
        return await show(cb, await screen_groups(child, db))
    async with db.session() as s:
        chat = await s.get(ChildChat, i)
    if chat is None or chat.child_id != child.id:
        await cb.answer("Grupo não encontrado.", show_alert=True)
        return
    f = await get_filter(db, child.id, i)

    async def save(**fields):
        async with db.session() as s:
            row = await s.get(WordFilter, f.id)
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    if a == "act" and 0 <= j < len(ACTIONS):
        await save(action=list(ACTIONS)[j])
    elif a == "del":
        await save(delete=not f.delete)
    elif a == "match":
        await save(match="contains" if f.match == "exact" else "exact")
    elif a == "wl":
        await save(warn_limit=f.warn_limit % 5 + 1)
    elif a == "wt":
        await save(warn_then={"mute": "kick", "kick": "ban", "ban": "mute"}[f.warn_then])
    elif a == "dur":
        await cb.answer()
        return await show(cb, screen_duration(f, i))
    elif a == "setdur" and j in DURATIONS:
        await save(duration_min=j)
    elif a == "add":
        return await ask(cb, state, "words_add", "➕ <b>Adicionar palavras</b>\n\nEnvie uma ou várias, uma por linha "
                         "ou separadas por vírgula. Pode ser frase (ex.: <code>chama no pv</code>).",
                         cbtn("❌ Cancelar", "wf", "", i), chat_row=i)
    elif a == "rem":
        return await ask(cb, state, "words_rem", "➖ <b>Remover palavras</b>\n\nEnvie as palavras que quer tirar "
                         "(uma por linha ou separadas por vírgula). Envie <code>*</code> para apagar a lista toda.",
                         cbtn("❌ Cancelar", "wf", "", i), chat_row=i)
    elif a == "list":
        words = word_list(f)
        body = "\n".join(f"• {escape(w)}" for w in words) or "<i>Lista vazia.</i>"
        await cb.answer()
        return await show(cb, (f"🔤 <b>Lista</b> · {escape(chat.title)}\n\n{body}"[:4000],
                               kb([cbtn("« Voltar", "wf", "", i)])))
    await cb.answer()
    await show(cb, await screen_filter(child, db, chat))


async def handle_input(kind: str, data: dict, message: Message, child: ChildBot, db: Database) -> Screen | str:
    i = data.get("chat_row", 0)
    async with db.session() as s:
        chat = await s.get(ChildChat, i)
    if chat is None or chat.child_id != child.id:
        return "Grupo não encontrado."
    f = await get_filter(db, child.id, i)
    raw = (message.text or "").strip()
    entries = [w.strip() for w in re.split(r"[\n,]+", raw) if w.strip()]
    words = word_list(f)
    if kind == "words_add":
        known = {normalize(w) for w in words}
        added = [w for w in entries if normalize(w) not in known]
        words += added
    elif raw == "*":
        words = []
    else:
        drop = {normalize(w) for w in entries}
        words = [w for w in words if normalize(w) not in drop]
    async with db.session() as s:
        row = await s.get(WordFilter, f.id)
        row.words = "\n".join(words)
        await s.commit()
    return await screen_filter(child, db, chat)


# ---------- aplicar no grupo ----------

async def _is_exempt(bot: Bot, db: Database, child: ChildBot, chat_id: int, user_id: int) -> bool:
    if await repo.is_child_admin(db, child, user_id):
        return True
    key = (bot.id, chat_id)
    cached = _admin_cache.get(key)
    if cached is None or time.monotonic() - cached[0] > 600:
        try:
            admins = {m.user.id for m in await bot.get_chat_administrators(chat_id)}
        except (TelegramBadRequest, TelegramForbiddenError):
            admins = set()
        _admin_cache[key] = (time.monotonic(), admins)
        cached = _admin_cache[key]
    return user_id in cached[1]


async def _notice(bot: Bot, db: Database, child: ChildBot, chat_id: int, text: str) -> None:
    try:
        m = await bot.send_message(chat_id, text)
    except (TelegramBadRequest, TelegramForbiddenError):
        return
    async with db.session() as s:
        s.add(PendingDelete(child_id=child.id, chat_id=chat_id, message_ids=str(m.message_id),
                            due_at=utcnow() + timedelta(seconds=NOTICE_SECONDS)))
        await s.commit()


async def punish(bot: Bot, chat_id: int, user_id: int, action: str, minutes: int) -> None:
    until = utcnow() + timedelta(minutes=minutes) if minutes else None
    if action == "mute":
        await bot.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False),
                                       until_date=until, use_independent_chat_permissions=True)
    elif action == "ban":
        await bot.ban_chat_member(chat_id, user_id, until_date=until)
    elif action == "kick":
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), ~F.text.startswith("/banirtodos"))
async def on_group_text(message: Message, bot: Bot, child: ChildBot, db: Database, settings) -> None:
    from .moderation import check_spam
    if await check_spam(message, bot, child, db):
        return
    user = message.from_user
    text = message.text or message.caption or ""
    if text and user is not None and not user.is_bot:
        async with db.session() as s:
            chat = await s.scalar(select(ChildChat).where(ChildChat.child_id == child.id,
                                                          ChildChat.chat_id == message.chat.id))
            f = await s.scalar(select(WordFilter).where(WordFilter.child_id == child.id,
                                                        WordFilter.chat_row_id == chat.id)) if chat else None
        if f is not None and f.action != "off":
            word = find_banned(text, f)
            if word and not await _is_exempt(bot, db, child, message.chat.id, user.id):
                await _apply(bot, db, child, message, f, word)
                return
    await on_group_message(bot, db, settings, child.id, message.chat.id)


async def _apply(bot: Bot, db: Database, child: ChildBot, message: Message, f: WordFilter, word: str = "") -> None:
    chat_id, user = message.chat.id, message.from_user
    where = escape(message.chat.title or str(chat_id))
    said = escape((message.text or message.caption or "")[:200])
    name = f'<a href="tg://user?id={user.id}">{escape(user.first_name or "usuário")}</a>'
    if f.delete:
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    action, minutes = f.action, f.duration_min
    if action == "warn":
        async with db.session() as s:
            w = await s.scalar(select(WarnCount).where(WarnCount.child_id == child.id, WarnCount.chat_id == chat_id,
                                                       WarnCount.user_id == user.id))
            if w is None:
                w = WarnCount(child_id=child.id, chat_id=chat_id, user_id=user.id, count=0)
                s.add(w)
            w.count += 1
            count = w.count
            if count >= f.warn_limit:
                w.count = 0
            await s.commit()
        if count < f.warn_limit:
            await _notice(bot, db, child, chat_id, f"⚠️ {name}, essa palavra não é permitida aqui. "
                                                   f"Advertência {count}/{f.warn_limit}.")
            await send_log(bot, db, child, f"⚠️ <b>Advertência {count}/{f.warn_limit}</b> · palavras proibidas\n\n"
                                           f"Quem: {who(user)}\nOnde: {where}\nPalavra: <code>{escape(word)}</code>\n"
                                           f"Mensagem: <i>{said}</i>", action_markup("warn", chat_id, user.id))
            return
        action = f.warn_then
    try:
        await punish(bot, chat_id, user.id, action, minutes if action != "kick" else 0)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.info("Palavras proibidas: não puni %s em %s: %s", user.id, chat_id, e)
        await send_log(bot, db, child, f"⚠️ <b>Não consegui punir</b> {who(user)} em {where}: {escape(e.message)}\n"
                                       "Confira se o bot é administrador com <b>Banir usuários</b>.")
        return
    title = {"mute": "🔇 Silenciado", "ban": "🚷 Banido", "kick": "❗ Expulso"}[action]
    span = f" por {interval_text(minutes)}" if minutes and action != "kick" else (" para sempre" if action != "kick" else "")
    await send_log(bot, db, child, f"<b>{title}{span}</b> · palavras proibidas\n\n"
                                   f"Quem: {who(user)}\nOnde: {where}\nPalavra: <code>{escape(word)}</code>\n"
                                   f"Mensagem: <i>{said}</i>", action_markup(action, chat_id, user.id))
    what = {"mute": "silenciado", "ban": "banido", "kick": "removido"}[action]
    extra = f" por {interval_text(minutes)}" if minutes and action != "kick" else ""
    await _notice(bot, db, child, chat_id, f"🚫 {name} foi {what}{extra}: palavra proibida.")


# ---------- botões do canal de registros ----------

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)


@router.callback_query(LG.filter())
async def on_log_button(cb: CallbackQuery, callback_data: LG, bot: Bot, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot.", show_alert=True)
        return
    a, chat_id, user_id = callback_data.a, callback_data.c, callback_data.u
    try:
        if a == "unmute":
            try:
                perms = getattr(await bot.get_chat(chat_id), "permissions", None) or FULL_PERMISSIONS
            except (TelegramBadRequest, TelegramForbiddenError):
                perms = FULL_PERMISSIONS
            await bot.restrict_chat_member(chat_id, user_id, perms, use_independent_chat_permissions=True)
            done = "🔊 Desmutado"
        elif a == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            done = "🚷 Banido"
        elif a == "unban":
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            done = "♻️ Desbanido"
        elif a == "reset":
            async with db.session() as s:
                w = await s.scalar(select(WarnCount).where(WarnCount.child_id == child.id, WarnCount.chat_id == chat_id,
                                                           WarnCount.user_id == user_id))
                if w:
                    w.count = 0
                    await s.commit()
            done = "🧹 Advertências zeradas"
        else:
            await cb.answer()
            return
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await cb.answer(f"Não consegui: {e.message}", show_alert=True)
        return
    await cb.answer(done + ".")
    if cb.message is not None and getattr(cb.message, "html_text", None):
        markup = action_markup("ban", chat_id, user_id) if a == "ban" else None
        try:
            await cb.message.edit_text(cb.message.html_text + f"\n\n{done} por {escape(cb.from_user.full_name)}",
                                       reply_markup=markup, link_preview_options={"is_disabled": True})
        except TelegramBadRequest:
            pass
