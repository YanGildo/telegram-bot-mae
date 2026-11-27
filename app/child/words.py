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
from sqlalchemy import delete, select

from .. import repo
from ..db import ChildBot, ChildChat, Database, PendingDelete, WarnCount, WordList, WordListChat, utcnow
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


def word_list(f) -> list[str]:
    return [w for w in (f.words or "").splitlines() if w.strip()]


def find_banned(text: str, f) -> str | None:
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


def penalty_text(f) -> str:
    if f.action == "off":
        return "Desativado"
    if f.action == "warn":
        then = {"mute": "silenciar", "kick": "expulsar", "ban": "banir"}[f.warn_then]
        return f"Advertência (com {f.warn_limit}: {then}" + (f" por {interval_text(f.duration_min)}" if f.duration_min and f.warn_then != "kick" else "") + ")"
    base = {"kick": "Kick (expulsa, pode voltar)", "mute": "Silenciar", "ban": "Ban"}[f.action]
    if f.action in ("mute", "ban"):
        base += f" por {interval_text(f.duration_min)}" if f.duration_min else " para sempre"
    return base


async def get_list(db: Database, child_id: int, list_id: int) -> WordList | None:
    async with db.session() as s:
        wl = await s.get(WordList, list_id)
    return wl if wl is not None and wl.child_id == child_id else None


async def list_chats(db: Database, wl: WordList) -> set[int]:
    async with db.session() as s:
        return set(await s.scalars(select(WordListChat.chat_row_id).where(WordListChat.list_id == wl.id)))


async def lists_for_chat(db: Database, child_id: int, chat_row_id: int) -> list[WordList]:
    """As listas que valem num grupo."""
    async with db.session() as s:
        picked = set(await s.scalars(select(WordListChat.list_id).where(WordListChat.chat_row_id == chat_row_id)))
        rows = list(await s.scalars(select(WordList).where(WordList.child_id == child_id).order_by(WordList.id)))
    return [w for w in rows if w.all_chats or w.id in picked]


def _groups(chats: list[ChildChat]) -> list[ChildChat]:
    return [c for c in chats if c.type != ChatType.CHANNEL and not c.partner_of
            and c.bot_status in ("administrator", "creator", "member")]


# ---------- telas ----------

async def screen_lists(child: ChildBot, db: Database) -> Screen:
    async with db.session() as s:
        lists = list(await s.scalars(select(WordList).where(WordList.child_id == child.id).order_by(WordList.id)))
    lines = ["🚫 <b>Palavras proibidas</b>\n",
             "Monte listas (ex.: Lista 1 = divulgação, Lista 2 = palavrões). Cada lista tem as palavras, a punição "
             "e os grupos onde vale: configure uma vez e mande para todos os grupos de uma vez.\n",
             "O bot precisa ser administrador com <b>Apagar mensagens</b> e <b>Banir usuários</b>. "
             "Admins do grupo e do bot nunca são punidos.\n"]
    rows = []
    for wl in lists:
        n = len(word_list(wl))
        g = "todos os grupos" if wl.all_chats else f"{len(await list_chats(db, wl))} grupo(s)"
        on = wl.action != "off" and n
        lines.append(f"{'✅' if on else '❌'} <b>{escape(wl.name)}</b> · {n} palavra(s) · {g} · {penalty_text(wl)}")
        rows.append([cbtn(f"{'✅' if on else '❌'} {wl.name[:40]}", "wl", "", wl.id)])
    if not lists:
        lines.append("<i>Nenhuma lista ainda. Toque em ➕ Nova lista.</i>")
    return "\n".join(lines)[:4000], kb([cbtn("➕ Nova lista", "words", "new", style="primary")], *rows,
                                        [cbtn("« Painel", "home")])


async def screen_list(child: ChildBot, db: Database, wl: WordList) -> Screen:
    i = wl.id
    chosen = await list_chats(db, wl)
    names = [c.title for c in _groups(await repo.child_chats(db, child.id)) if c.id in chosen]
    where = "🌐 todos os grupos (inclusive os novos)" if wl.all_chats else \
        (", ".join(escape(n) for n in names[:8]) + (f" e mais {len(names) - 8}" if len(names) > 8 else "")
         if names else "❌ nenhum: escolha em 📍 Grupos")
    text = (f"🔤 <b>{escape(wl.name)}</b>\n\n"
            f"Penalidade: <b>{penalty_text(wl)}</b>\n"
            f"Apagar a mensagem: {'Sim ✅' if wl.delete else 'Não ❌'}\n"
            f"Busca: {'palavra exata' if wl.match == 'exact' else 'contém o termo'} · ignora acentos e maiúsculas\n"
            f"Palavras na lista: {len(word_list(wl))}\n"
            f"📍 Vale em: {where}\n\n"
            "<b>O que cada botão faz</b>\n"
            "• ❌ ❕ ❗ 🔇 🚷: escolhe a punição.\n"
            "• 🗑 Apagar mensagens: apaga ou não a mensagem com a palavra.\n"
            "• ⏱ Duração: por quanto tempo vale o silenciar/ban.\n"
            "• 🔎 Busca: <i>contém</i> pega 'promo' dentro de 'promoção'; <i>exata</i> só a palavra inteira.\n"
            "• ➕ / ➖: adiciona ou tira palavras (várias de uma vez, uma por linha ou separadas por vírgula).\n"
            "• 📍 Grupos: onde esta lista vale.")

    def act(k):
        return cbtn(("✅ " if wl.action == k else "") + ACTIONS[k], "wl", "act", i, j=list(ACTIONS).index(k))
    rows = [[act("off"), act("warn"), act("kick")], [act("mute"), act("ban")],
            [cbtn(f"🗑 Apagar mensagens {'✅' if wl.delete else '❌'}", "wl", "del", i)],
            [cbtn("⏱ Definir a duração", "wl", "dur", i)]]
    if wl.action == "warn":
        rows.append([cbtn(f"⚠️ Advertências até punir: {wl.warn_limit}", "wl", "wl", i),
                     cbtn("Depois: " + {"mute": "🔇", "kick": "❗", "ban": "🚷"}[wl.warn_then], "wl", "wt", i)])
    rows += [[cbtn("🔎 Busca: " + ("exata" if wl.match == "exact" else "contém"), "wl", "match", i)],
             [cbtn("➕ Adicionar", "wl", "add", i), cbtn("➖ Remover", "wl", "rem", i)],
             [cbtn(f"🔤 Ver palavras ({len(word_list(wl))})", "wl", "list", i)],
             [cbtn("📍 Grupos " + ("(todos)" if wl.all_chats else f"({len(names)})"), "wld", "", i, style="primary")],
             [cbtn("✏️ Nome", "wl", "name", i), cbtn("🗑 Apagar lista", "wl", "kill1", i)],
             [cbtn("« Listas", "words")]]
    return text, kb(*rows)


async def screen_dests(child: ChildBot, db: Database, wl: WordList) -> Screen:
    chosen = await list_chats(db, wl)
    groups = _groups(await repo.child_chats(db, child.id))
    text = (f"📍 <b>Grupos</b> · {escape(wl.name)}\n\n"
            "Escolha onde esta lista vale. <b>🌐 Todos os grupos</b> inclui também os grupos que você adicionar "
            "depois.")
    rows = [[cbtn(("✅ " if wl.all_chats else "▫️ ") + "🌐 Todos os grupos", "wld", "all", wl.id,
                  style="success" if wl.all_chats else None)]]
    if not wl.all_chats:
        rows += [[cbtn(("✅ " if c.id in chosen else "▫️ ") + (c.title or str(c.chat_id))[:40], "wld", "tog", wl.id,
                       j=c.id)] for c in groups]
    if not groups:
        text += "\n\n<i>O bot ainda não está em nenhum grupo.</i>"
    return text, kb(*rows, [cbtn("« Voltar", "wl", "", wl.id)])


def screen_duration(f: WordList, i: int) -> Screen:
    text = ("⏱ <b>Duração da punição</b>\n\nVale para silenciar e ban (também quando a advertência chega no limite). "
            "Kick não tem duração: a pessoa sai e pode voltar.")
    btns = [cbtn(("✅ " if f.duration_min == d else "") + (interval_text(d) if d else "Para sempre"), "wl", "setdur", i, j=d)
            for d in DURATIONS]
    return text, kb(btns[:3], btns[3:6], btns[6:], [cbtn("« Voltar", "wl", "", i)])


# ---------- botões ----------

@router.callback_query(CP.filter(F.s.in_({"words", "wl", "wld"})))
async def on_words(cb: CallbackQuery, callback_data: CP, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    s_, a, i, j = callback_data.s, callback_data.a, callback_data.i, callback_data.j
    await state.clear()
    if s_ == "words":
        if a == "new":
            async with db.session() as s:
                n = len(list(await s.scalars(select(WordList.id).where(WordList.child_id == child.id))))
                wl = WordList(child_id=child.id, name=f"Lista {n + 1}")
                s.add(wl)
                await s.commit()
            await cb.answer()
            return await show(cb, await screen_list(child, db, wl))
        await cb.answer()
        return await show(cb, await screen_lists(child, db))
    wl = await get_list(db, child.id, i)
    if wl is None:
        await cb.answer("Lista não encontrada.", show_alert=True)
        return await show(cb, await screen_lists(child, db))

    async def save(**fields):
        async with db.session() as s:
            row = await s.get(WordList, wl.id)
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    if s_ == "wld":
        if a == "all":
            await save(all_chats=not wl.all_chats)
        elif a == "tog":
            async with db.session() as s:
                c = await s.get(ChildChat, j)
                if c is None or c.child_id != child.id:
                    await cb.answer("Grupo não encontrado.", show_alert=True)
                    return
                row = await s.scalar(select(WordListChat).where(WordListChat.list_id == wl.id,
                                                                WordListChat.chat_row_id == j))
                if row:
                    await s.delete(row)
                else:
                    s.add(WordListChat(list_id=wl.id, chat_row_id=j))
                await s.commit()
        await cb.answer()
        return await show(cb, await screen_dests(child, db, await get_list(db, child.id, i)))

    if a == "act" and 0 <= j < len(ACTIONS):
        await save(action=list(ACTIONS)[j])
    elif a == "del":
        await save(delete=not wl.delete)
    elif a == "match":
        await save(match="contains" if wl.match == "exact" else "exact")
    elif a == "wl":
        await save(warn_limit=wl.warn_limit % 5 + 1)
    elif a == "wt":
        await save(warn_then={"mute": "kick", "kick": "ban", "ban": "mute"}[wl.warn_then])
    elif a == "dur":
        await cb.answer()
        return await show(cb, screen_duration(wl, i))
    elif a == "setdur" and j in DURATIONS:
        await save(duration_min=j)
    elif a == "add":
        return await ask(cb, state, "words_add", "➕ <b>Adicionar palavras</b>\n\nEnvie uma ou várias, uma por linha "
                         "ou separadas por vírgula. Pode ser frase (ex.: <code>chama no pv</code>).",
                         cbtn("❌ Cancelar", "wl", "", i), list_id=i)
    elif a == "rem":
        return await ask(cb, state, "words_rem", "➖ <b>Remover palavras</b>\n\nEnvie as palavras que quer tirar "
                         "(uma por linha ou separadas por vírgula). Envie <code>*</code> para apagar a lista toda.",
                         cbtn("❌ Cancelar", "wl", "", i), list_id=i)
    elif a == "name":
        return await ask(cb, state, "words_name", "✏️ <b>Nome da lista</b>\n\nEx.: <code>Divulgação</code>.",
                         cbtn("❌ Cancelar", "wl", "", i), list_id=i)
    elif a == "list":
        words = word_list(wl)
        body = "\n".join(f"• {escape(w)}" for w in words) or "<i>Lista vazia.</i>"
        await cb.answer()
        return await show(cb, (f"🔤 <b>{escape(wl.name)}</b>\n\n{body}"[:4000], kb([cbtn("« Voltar", "wl", "", i)])))
    elif a == "kill1":
        await cb.answer()
        return await show(cb, (f"🗑 <b>Apagar «{escape(wl.name)}»?</b>\n\nAs palavras dela deixam de valer em todos "
                               "os grupos.", kb([cbtn("🗑 Sim, apagar", "wl", "kill2", i, style="danger"),
                                                 cbtn("« Voltar", "wl", "", i)])))
    elif a == "kill2":
        async with db.session() as s:
            await s.execute(delete(WordListChat).where(WordListChat.list_id == wl.id))
            await s.execute(delete(WordList).where(WordList.id == wl.id))
            await s.commit()
        await cb.answer("Lista apagada.")
        return await show(cb, await screen_lists(child, db))
    await cb.answer()
    await show(cb, await screen_list(child, db, await get_list(db, child.id, i)))


async def handle_input(kind: str, data: dict, message: Message, child: ChildBot, db: Database) -> Screen | str:
    wl = await get_list(db, child.id, data.get("list_id", 0))
    if wl is None:
        return "Lista não encontrada."
    raw = (message.text or "").strip()
    if kind == "words_name":
        if not raw:
            return "Envie o nome ou /cancelar."
        async with db.session() as s:
            row = await s.get(WordList, wl.id)
            row.name = raw[:60]
            await s.commit()
        return await screen_list(child, db, await get_list(db, child.id, wl.id))
    entries = [w.strip() for w in re.split(r"[\n,]+", raw) if w.strip()]
    words = word_list(wl)
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
        row = await s.get(WordList, wl.id)
        row.words = "\n".join(words)
        await s.commit()
    return await screen_list(child, db, await get_list(db, child.id, wl.id))


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
    async with db.session() as s:
        partner_row = await s.scalar(select(ChildChat.id).where(ChildChat.child_id == child.id,
                                                                ChildChat.chat_id == message.chat.id,
                                                                ChildChat.partner_of.is_not(None)))
    if partner_row is not None:  # grupo de parceiro: só conta mensagens, não modera
        await on_group_message(bot, db, settings, child.id, message.chat.id)
        return
    if await check_spam(message, bot, child, db):
        return
    user = message.from_user
    text = message.text or message.caption or ""
    if text and user is not None and not user.is_bot:
        async with db.session() as s:
            chat = await s.scalar(select(ChildChat).where(ChildChat.child_id == child.id,
                                                          ChildChat.chat_id == message.chat.id))
        if chat is not None and chat.f_words is not False:
            for f in await lists_for_chat(db, child.id, chat.id):
                if f.action == "off":
                    continue
                word = find_banned(text, f)
                if word and not await _is_exempt(bot, db, child, message.chat.id, user.id):
                    await _apply(bot, db, child, message, f, word)
                    return
    await on_group_message(bot, db, settings, child.id, message.chat.id)


async def _apply(bot: Bot, db: Database, child: ChildBot, message: Message, f, word: str = "") -> None:
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
