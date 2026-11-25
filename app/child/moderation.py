"""Moderação do bot filho: ban em todos os grupos e anti-spam por @.

Ban em todos: quando o dono ou um admin do bot (ou um admin anônimo) bane alguém pelo Telegram em um
grupo onde o bot é administrador, o bot bane a pessoa pelo ID em todos os outros. Também por comando:
/banirtodos ID (ou respondendo à pessoa no grupo) e /desbanirtodos ID.
Anti-spam: apaga mensagens de @ da lista (quem postou, repasse, ou texto/botões divulgando o @).
"""

import asyncio
import logging
import re
from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           User as TgUser)
from sqlalchemy import func, select

from .. import repo
from ..db import ChildBan, ChildBot, ChildChat, Database, PendingDelete, utcnow
from ..ui import CP, Screen, cbtn, kb, show
from .flow import ask
from .logs import send_log, who

logger = logging.getLogger(__name__)
router = Router(name="moderacao")

ANON_ADMIN_ID = 1087968824  # "GroupAnonymousBot": autor quando um admin age anônimo
DELAYS = (0, 60, 300, 600)
_warned: set[tuple[int, int]] = set()


class BA(CallbackData, prefix="ba"):
    """Botão ♻️ Desbanir de todos (canal de registros)."""
    u: int


def unban_all_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="♻️ Desbanir de todos", callback_data=BA(u=user_id).pack())]])


async def _fresh(db: Database, child_id: int) -> ChildBot:
    async with db.session() as s:
        return await s.get(ChildBot, child_id)


async def network(db: Database, child: ChildBot) -> list[ChildChat]:
    row = await _fresh(db, child.id)
    return [c for c in await repo.child_chats(db, child.id)
            if c.bot_status in ("administrator", "creator") and c.chat_id != row.log_chat_id]


async def _call(fn):
    try:
        return await fn()
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await fn()


# ---------- ban em todos ----------

async def ban_everywhere(bot: Bot, db: Database, child: ChildBot, target_id: int, name: str, username: str | None,
                         actor_label: str, source_chat_id: int | None, source_title: str,
                         target_is_bot: bool = False) -> str:
    chats = await network(db, child)
    if source_chat_id is not None and source_chat_id not in {c.chat_id for c in chats}:
        return "esse grupo não está na rede (o bot não é administrador lá)"
    if target_is_bot or target_id == bot.id or await repo.is_child_admin(db, child, target_id):
        return "não bano bots nem os admins do bot"
    async with db.session() as s:
        ban = await s.scalar(select(ChildBan).where(ChildBan.child_id == child.id, ChildBan.user_id == target_id))
        if ban is None:
            ban = ChildBan(child_id=child.id, user_id=target_id)
            s.add(ban)
        ban.name, ban.username, ban.active = name or ban.name or "", username or ban.username, True
        ban.source_title, ban.actor = source_title[:250], actor_label[:250]
        await s.commit()
    ok, failed = [], []
    for c in chats:
        if c.chat_id == source_chat_id:
            continue
        try:
            await _call(lambda: bot.ban_chat_member(c.chat_id, target_id))
            ok.append(c.title or str(c.chat_id))
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            failed.append(f"{c.title or c.chat_id}: {e.message}")
    label = (escape(name) if name else "sem nome") + (f" (@{escape(username)})" if username else "")
    await send_log(bot, db, child,
                   "🚫 <b>Ban em todos os grupos</b>\n\n"
                   f"Quem: {label} · <code>{target_id}</code>\n"
                   f"Onde foi banido: {escape(source_title or 'comando /banirtodos')}\n"
                   f"Por: {actor_label}\n\n"
                   f"Banido em {len(ok)} grupo(s)/canal(is)" + (":\n" + "\n".join(f"✅ {escape(x)}" for x in ok) if ok else ".")
                   + ("\n\nNão consegui em:\n" + "\n".join(f"⚠️ {escape(x)}" for x in failed) if failed else ""),
                   unban_all_markup(target_id))
    logger.info("@%s: ban em todos de %s (%s ok, %s falhas)", child.username, target_id, len(ok), len(failed))
    return f"banido em {len(ok)} grupo(s)/canal(is)" + (f", {len(failed)} falharam" if failed else "")


async def unban_everywhere(bot: Bot, db: Database, child: ChildBot, user_id: int) -> tuple[int, int]:
    async with db.session() as s:
        ban = await s.scalar(select(ChildBan).where(ChildBan.child_id == child.id, ChildBan.user_id == user_id))
        if ban is not None:
            ban.active = False
            await s.commit()
    ok = fail = 0
    for c in await network(db, child):
        try:
            await _call(lambda: bot.unban_chat_member(c.chat_id, user_id, only_if_banned=True))
            ok += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            fail += 1
    return ok, fail


async def apply_bans_to_chat(bot: Bot, db: Database, child: ChildBot, chat_id: int) -> int:
    """Grupo novo na rede: aplica os bans que já existem."""
    row = await _fresh(db, child.id)
    if not row.ban_network:
        return 0
    async with db.session() as s:
        ids = list(await s.scalars(select(ChildBan.user_id).where(ChildBan.child_id == child.id,
                                                                    ChildBan.active.is_(True))))
    n = 0
    for uid in ids:
        try:
            await _call(lambda: bot.ban_chat_member(chat_id, uid))
            n += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    return n


@router.chat_member()
async def on_member_banned(event: ChatMemberUpdated, bot: Bot, child: ChildBot, db: Database) -> None:
    if event.new_chat_member.status != "kicked" or event.old_chat_member.status == "kicked":
        return
    actor, target, chat = event.from_user, event.new_chat_member.user, event.chat
    if actor and actor.id == bot.id:
        return  # ban feito pelo próprio bot (propagação): não repete
    if not (await _fresh(db, child.id)).ban_network:
        return
    anonymous = actor is not None and actor.id == ANON_ADMIN_ID
    if actor is None or (not anonymous and (actor.is_bot or not await repo.is_child_admin(db, child, actor.id))):
        logger.info("@%s: ban de %s em %s não espalhado (feito por %s)", child.username, target.id, chat.id,
                    actor.id if actor else None)
        return
    await ban_everywhere(bot, db, child, target.id, target.full_name or "", target.username,
                         "admin anônimo" if anonymous else who(actor), chat.id, chat.title or "",
                         target_is_bot=target.is_bot)


@router.message(Command("banirtodos", "desbanirtodos"))
async def cmd_ban_all(message: Message, command: CommandObject, bot: Bot, child: ChildBot, db: Database) -> None:
    user = message.from_user
    if user is None or not await repo.is_child_admin(db, child, user.id):
        return
    if message.chat.type != ChatType.PRIVATE:
        try:
            await message.delete()  # o comando some do grupo na hora
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    target_id, name, username, is_bot = None, "", None, False
    arg = (command.args or "").strip().split()
    if arg and arg[0].lstrip("-").isdigit():
        target_id = int(arg[0])
    elif message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        t = message.reply_to_message.from_user
        target_id, name, username, is_bot = t.id, t.full_name, t.username, t.is_bot

    async def dm(text: str) -> None:
        try:
            await bot.send_message(user.id, text)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    if target_id is None:
        await dm("Use <code>/banirtodos 123456789</code> com o ID da pessoa, ou responda a uma mensagem dela no "
                 "grupo com <code>/banirtodos</code>. Para desfazer: <code>/desbanirtodos 123456789</code>.")
        return
    if command.command == "desbanirtodos":
        ok, fail = await unban_everywhere(bot, db, child, target_id)
        await send_log(bot, db, child, f"♻️ <code>{target_id}</code> desbanido de todos por {who(user)}.")
        await dm(f"♻️ <code>{target_id}</code> desbanido em {ok} grupo(s)/canal(is)." + (f" {fail} falharam." if fail else ""))
        return
    summary = await ban_everywhere(bot, db, child, target_id, name, username, who(user), None,
                                   message.chat.title or "" if message.chat.type != ChatType.PRIVATE else "",
                                   target_is_bot=is_bot)
    await dm(f"🚫 <code>{target_id}</code>: {escape(summary)}.")


@router.callback_query(BA.filter())
async def on_unban_all(cb: CallbackQuery, callback_data: BA, bot: Bot, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot.", show_alert=True)
        return
    ok, fail = await unban_everywhere(bot, db, child, callback_data.u)
    await cb.answer(f"Desbanido em {ok} grupo(s)." + (f" {fail} falharam." if fail else ""), show_alert=True)
    if cb.message is not None and getattr(cb.message, "html_text", None):
        try:
            await cb.message.edit_text(cb.message.html_text + f"\n\n♻️ <b>Desbanido de todos</b> por "
                                       f"{escape(cb.from_user.full_name)}", reply_markup=None,
                                       link_preview_options={"is_disabled": True})
        except TelegramBadRequest:
            pass


# ---------- anti-spam ----------

def parse_usernames(text: str) -> list[str]:
    out = []
    for raw in re.split(r"[\s,;]+", text or ""):
        name = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", raw.strip(), flags=re.I).strip("/").lower()
        if re.fullmatch(r"[a-z0-9_]{4,32}", name) and name not in out:
            out.append(name)
    return out


def spam_list(row: ChildBot) -> list[str]:
    return [x for x in (row.spam_names or "").splitlines() if x.strip()]


def _senders(message: Message) -> set[str]:
    names = set()
    for w in (message.from_user, message.sender_chat, getattr(message, "via_bot", None)):
        if w is not None and getattr(w, "username", None):
            names.add(w.username.lower())
        for extra in getattr(w, "active_usernames", None) or []:
            names.add(extra.lower())
    origin = message.forward_origin
    if origin is not None:
        for w in (getattr(origin, "sender_user", None), getattr(origin, "chat", None)):
            if w is not None and getattr(w, "username", None):
                names.add(w.username.lower())
    return names


def _mentions(message: Message) -> set[str]:
    found = []
    for text, entities in ((message.text, message.entities), (message.caption, message.caption_entities)):
        if not text:
            continue
        for e in entities or []:
            if e.type in ("mention", "url"):
                found.append(e.extract_from(text))
            elif e.type == "text_link" and e.url:
                found.append(e.url)
    markup = message.reply_markup
    for row in (markup.inline_keyboard if markup is not None and hasattr(markup, "inline_keyboard") else []):
        for b in row:
            if b.url:
                found.append(b.url)
    names = set()
    for raw in found:
        raw = raw.strip()
        low = raw.lower()
        if raw.startswith("@") or "t.me/" in low or "telegram.me/" in low:
            raw = raw.split("?")[0]
            if "t.me/" in low or "telegram.me/" in low:
                raw = raw.lower().split("me/", 1)[1].split("/")[0]
            names.update(parse_usernames(raw))
    return names


async def check_spam(message: Message, bot: Bot, child: ChildBot, db: Database) -> bool:
    """True se a mensagem é de/divulga um @ da lista (e já foi apagada ou agendada para apagar)."""
    row = await _fresh(db, child.id)
    blocked = set(spam_list(row))
    if not blocked:
        return False
    hit = (_senders(message) | _mentions(message)) & blocked
    if not hit or (message.from_user and await repo.is_child_admin(db, child, message.from_user.id)):
        return False
    delay = row.spam_delay if row.spam_delay is not None else 60
    if delay > 0:
        async with db.session() as s:
            s.add(PendingDelete(child_id=child.id, chat_id=message.chat.id, message_ids=str(message.message_id),
                                due_at=utcnow() + timedelta(seconds=delay)))
            r = await s.get(ChildBot, child.id)
            r.spam_deleted = (r.spam_deleted or 0) + 1
            await s.commit()
        return True
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        key = (child.id, message.chat.id)
        if key not in _warned:
            _warned.add(key)
            await send_log(bot, db, child, f"⚠️ <b>Anti-spam</b>: não consegui apagar a mensagem de "
                                           f"@{escape(', @'.join(sorted(hit)))} em {escape(message.chat.title or '')}: "
                                           f"{escape(e.message)}. Confira se o bot pode <b>Apagar mensagens</b>.",
                           owner_fallback=True)
        return True
    async with db.session() as s:
        r = await s.get(ChildBot, child.id)
        r.spam_deleted = (r.spam_deleted or 0) + 1
        await s.commit()
    return True


@router.edited_message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_edited(message: Message, bot: Bot, child: ChildBot, db: Database) -> None:
    if not await check_spam(message, bot, child, db):
        raise SkipHandler()


# ---------- telas ----------

async def screen_ban(child: ChildBot, db: Database) -> Screen:
    row = await _fresh(db, child.id)
    chats = await network(db, child)
    async with db.session() as s:
        active = await s.scalar(select(func.count(ChildBan.id)).where(ChildBan.child_id == child.id,
                                                                      ChildBan.active.is_(True))) or 0
    no_ban = [c for c in chats if c.type != ChatType.CHANNEL and not c.can_restrict and c.bot_status != "creator"]
    text = ("🚫 <b>Ban em todos os grupos</b>\n\n"
            "Quando <b>você</b> (ou um admin do bot) bane alguém pelo Telegram num grupo onde o bot é "
            "administrador, o bot bane a mesma pessoa <b>pelo ID</b> em todos os outros. Vale também para "
            "admin anônimo. Bans de outros bots (ex.: Group Help) e de outros admins dos grupos não espalham. "
            "Os admins do bot nunca são banidos. Grupo novo já recebe os bans que existem.\n\n"
            f"Situação: <b>{'ligado ✅' if row.ban_network else 'desligado ❌'}</b>\n"
            f"Grupos e canais na rede: {len(chats)}\n"
            f"Banidos em todos: {active}\n"
            + (f"⚠️ Sem permissão de banir em: {', '.join(escape(c.title) for c in no_ban)}\n" if no_ban else "")
            + "\n<b>Comandos</b> (sem precisar de @)\n"
            "• <code>/banirtodos 123456789</code> no privado do bot\n"
            "• responder a uma mensagem da pessoa no grupo com <code>/banirtodos</code> (o comando some)\n"
            "• <code>/desbanirtodos 123456789</code> para desfazer\n\n"
            "Cada ban vai para o 📋 canal de registros com ♻️ Desbanir de todos.\n"
            "• 📥 Importar banidos: cole uma lista de IDs (ex.: a que o bot de clipes manda no /exportar).")
    return text, kb([cbtn("⏸ Desligar" if row.ban_network else "▶️ Ligar", "ban", "tog")],
                    [cbtn("📥 Importar banidos", "ban", "imp")],
                    [cbtn("« Painel", "home")])


async def screen_spam(child: ChildBot, db: Database) -> Screen:
    row = await _fresh(db, child.id)
    names = spam_list(row)
    delay = row.spam_delay if row.spam_delay is not None else 60
    label = lambda d: "na hora" if d == 0 else f"{d // 60} min"  # noqa: E731
    text = ("🧹 <b>Anti-spam</b>\n\n"
            "Apaga nos grupos onde o bot é admin: mensagens postadas por estes @, repasses deles, o aviso de "
            "\"fixou uma mensagem\" e qualquer mensagem cujo texto ou botões divulguem o @ ou o link t.me deles.\n"
            f"Apagar: <b>{label(delay)}</b> depois de postada.\n"
            "Requisito: o bot precisa poder <b>Apagar mensagens</b>. O que outro <b>bot</b> posta não chega ao "
            "seu bot (limite do Telegram). Os admins do bot nunca têm mensagem apagada.\n\n"
            + ("\n".join(f"• @{escape(n)}" for n in names) if names else "<i>Lista vazia.</i>")
            + f"\n\nMensagens apagadas até agora: {row.spam_deleted or 0}\nToque num @ para tirar da lista.")
    delays = [cbtn(("✅ " if d == delay else "") + label(d).capitalize(), "spam", "delay", d) for d in DELAYS]
    rows = [[cbtn(f"🗑 @{n}"[:60], "spam", "del", k)] for k, n in enumerate(names)]
    return text, kb([cbtn("➕ Adicionar @", "spam", "add")], delays, *rows, [cbtn("« Painel", "home")])


@router.callback_query(CP.filter(F.s.in_({"ban", "spam"})))
async def on_screens(cb: CallbackQuery, callback_data: CP, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    s_, a, i = callback_data.s, callback_data.a, callback_data.i
    await state.clear()
    note = None
    if s_ == "ban":
        if a == "tog":
            async with db.session() as s:
                r = await s.get(ChildBot, child.id)
                r.ban_network = not r.ban_network
                await s.commit()
                note = "Ligado." if r.ban_network else "Desligado."
        elif a == "imp":
            return await ask(cb, state, "mod_import", "📥 <b>Importar banidos</b>\n\nCole os IDs (números), um por "
                             "linha ou separados por vírgula. O bot registra e bane cada um em todos os grupos.",
                             cbtn("❌ Cancelar", "ban"))
        await cb.answer(note)
        return await show(cb, await screen_ban(child, db))
    if a == "add":
        return await ask(cb, state, "mod_spam", "Envie um ou mais @ para apagar as mensagens deles nos grupos.\n"
                         "Ex.: <code>@listadivulga @outrocanal</code> (pode ser link t.me também).",
                         cbtn("❌ Cancelar", "spam"))
    async with db.session() as s:
        r = await s.get(ChildBot, child.id)
        names = spam_list(r)
        if a == "del" and 0 <= i < len(names):
            note = f"@{names.pop(i)} saiu da lista."
            r.spam_names = "\n".join(names)
        elif a == "delay" and i in DELAYS:
            r.spam_delay = i
        await s.commit()
    await cb.answer(note)
    await show(cb, await screen_spam(child, db))


async def handle_input(kind: str, message: Message, bot: Bot, child: ChildBot, db: Database) -> Screen | str:
    raw = message.text or ""
    if kind == "mod_spam":
        new = parse_usernames(raw)
        if not new:
            return "Não achei nenhum @ válido. Ex.: <code>@listadivulga</code>, ou /cancelar."
        async with db.session() as s:
            r = await s.get(ChildBot, child.id)
            names = spam_list(r)
            r.spam_names = "\n".join(names + [n for n in new if n not in names])
            await s.commit()
        return await screen_spam(child, db)
    ids = [int(x) for x in re.findall(r"\d{3,}", raw)]
    if not ids:
        return "Não achei nenhum ID. Cole os números, um por linha, ou /cancelar."
    user = message.from_user
    total = 0
    for uid in dict.fromkeys(ids):
        summary = await ban_everywhere(bot, db, child, uid, "", None, f"importação por {who(user)}", None, "importação")
        total += summary.startswith("banido")
    await bot.send_message(message.chat.id, f"📥 {total} ID(s) importado(s) e banido(s) em todos os grupos.")
    return await screen_ban(child, db)
