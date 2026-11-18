"""Bot filho: painel do dono, grupos e canais, admins do bot, entrada e saída de grupos."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy import delete, select

from .. import repo
from ..db import ChildAdmin, ChildBot, ChildChat, Database, utcnow
from ..manager import BotManager
from ..ui import CP, Screen, cbtn, kb, send, show, url

logger = logging.getLogger(__name__)
router = Router(name="filho")

GROUP_RIGHTS = "change_info+delete_messages+restrict_members+invite_users+pin_messages+manage_topics"
CHANNEL_RIGHTS = "post_messages+edit_messages+delete_messages+invite_users"


class ChildFlow(StatesGroup):
    add_admin = State()


def mae_link(manager: BotManager, param: str) -> str:
    return f"https://t.me/{manager.mother_username}?start={param}"


# ---------- permissões ----------

def missing_rights(c: ChildChat) -> list[str]:
    if c.bot_status == "creator":
        return []
    if c.bot_status != "administrator":
        return ["ser administrador"]
    if c.type == ChatType.CHANNEL:
        need = (("postar mensagens", c.can_post), ("apagar mensagens", c.can_delete))
    else:
        need = (("apagar mensagens", c.can_delete), ("banir usuários", c.can_restrict))
    return [name for name, ok in need if not ok]


def chat_icon(c: ChildChat) -> str:
    if c.bot_status not in ("administrator", "creator"):
        return "❌"
    return "⚠️" if missing_rights(c) else "✅"


def _apply_member(c: ChildChat, member) -> None:
    c.bot_status = member.status
    c.can_delete = bool(getattr(member, "can_delete_messages", False))
    c.can_restrict = bool(getattr(member, "can_restrict_members", False))
    c.can_post = bool(getattr(member, "can_post_messages", False))
    c.can_pin = bool(getattr(member, "can_pin_messages", False))
    c.checked_at = utcnow()


async def refresh_rights(bot: Bot, db: Database, child: ChildBot) -> int:
    """Pergunta ao Telegram as permissões do bot em cada grupo/canal."""
    n = 0
    for c in await repo.child_chats(db, child.id):
        try:
            member = await bot.get_chat_member(c.chat_id, bot.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            member = None
        async with db.session() as s:
            row = await s.get(ChildChat, c.id)
            if member is None:
                row.bot_status, row.checked_at = "left", utcnow()
            else:
                _apply_member(row, member)
            await s.commit()
        n += 1
    return n


# ---------- telas ----------

async def screen_home(child: ChildBot, db: Database) -> Screen:
    chats = await repo.child_chats(db, child.id)
    problems = sum(1 for c in chats if chat_icon(c) != "✅")
    text = (f"⚙️ <b>Painel do @{escape(child.username)}</b>\n\n"
            "Daqui você configura tudo deste bot. Toque numa opção:\n\n"
            "• 👥 <b>Grupos e canais</b>: onde o bot está e se ele tem as permissões certas\n"
            "• 🗂 <b>Acervo</b>: fotos e vídeos postados sozinhos, em ordem ou aleatório <i>(em breve)</i>\n"
            "• 🔁 <b>Mensagens repetidas</b>: posts programados, sem limite <i>(em breve)</i>\n"
            "• 🚫 <b>Palavras proibidas</b>: censura no chat com punição <i>(em breve)</i>\n"
            "• 👮 <b>Admins do bot</b>: quem mais pode usar este painel")
    if problems:
        text += f"\n\n⚠️ {problems} grupo(s)/canal(is) com permissão faltando. Veja em 👥 Grupos e canais."
    return text, kb([cbtn(f"👥 Grupos e canais ({len(chats)})", "chats")],
                    [cbtn("🗂 Acervo", "soon", "acervo"), cbtn("🔁 Mensagens repetidas", "soon", "rep")],
                    [cbtn("🚫 Palavras proibidas", "soon", "words"), cbtn("👮 Admins do bot", "admins")],
                    [cbtn("❓ Ajuda", "help")])


SOON = {
    "acervo": ("🗂 <b>Acervo</b>",
               "Você posta fotos e vídeos num canal privado e o bot publica sozinho nos seus grupos, "
               "em ordem ou aleatório, sem repetir até usar todos. Vai dar para limitar a quantidade "
               "de mídias, trocar as antigas pelas novas e zerar o acervo."),
    "rep": ("🔁 <b>Mensagens repetidas</b>",
            "Mensagens com texto, mídia e botões que saem sozinhas de tempos em tempos, "
            "nos horários e dias que você escolher. Sem limite de quantidade."),
    "words": ("🚫 <b>Palavras proibidas</b>",
              "Listas de palavras sem limite. Quem usar leva a punição que você escolher: "
              "advertência, silenciar, expulsar ou banir, apagando ou não a mensagem."),
}


def screen_soon(key: str) -> Screen:
    title, desc = SOON.get(key, ("🚧", ""))
    return f"{title}\n\n{desc}\n\n🚧 Essa função chega na próxima etapa.", kb([cbtn("« Painel", "home")])


async def screen_chats(child: ChildBot, db: Database) -> Screen:
    chats = await repo.child_chats(db, child.id)
    u = child.username
    lines = ["👥 <b>Grupos e canais</b>\n",
             "Para o bot funcionar, ele precisa ser <b>administrador</b>. Use os botões ➕ abaixo: "
             "o Telegram já abre com as permissões certas marcadas.\n",
             "✅ tudo certo · ⚠️ falta permissão · ❌ não é administrador\n"]
    for c in chats:
        miss = missing_rights(c)
        kind = "canal" if c.type == ChatType.CHANNEL else "grupo"
        lines.append(f"{chat_icon(c)} {escape(c.title or str(c.chat_id))} ({kind})"
                     + (f" — falta: {', '.join(miss)}" if miss else ""))
    if not chats:
        lines.append("<i>O bot ainda não está em nenhum grupo ou canal.</i>")
    lines.append("\n<b>O que cada botão faz</b>\n"
                 "• ➕ Adicionar: escolhe o grupo ou canal e já marca as permissões.\n"
                 "• 🔄 Verificar permissões: confere de novo no Telegram.\n"
                 "• Toque num grupo para ver os detalhes ou tirar o bot de lá.\n\n"
                 "Só você e os admins do bot podem adicionar o bot. Se outra pessoa tentar, ele sai na hora.")
    rows = [[url("➕ Adicionar a um grupo", f"https://t.me/{u}?startgroup=painel&admin={GROUP_RIGHTS}")],
            [url("➕ Adicionar a um canal", f"https://t.me/{u}?startchannel=true&admin={CHANNEL_RIGHTS}")]]
    rows += [[cbtn(f"{chat_icon(c)} {(c.title or str(c.chat_id))[:40]}", "chat", i=c.id)] for c in chats]
    rows += [[cbtn("🔄 Verificar permissões", "chats", "check")], [cbtn("« Painel", "home")]]
    return "\n".join(lines), kb(*rows)


def screen_chat(c: ChildChat) -> Screen:
    kind = "Canal" if c.type == ChatType.CHANNEL else "Grupo"
    yes = lambda ok: "✅" if ok else "❌"  # noqa: E731
    status = {"administrator": "administrador", "creator": "dono", "member": "membro (sem ser admin)"}.get(
        c.bot_status, c.bot_status)
    lines = [f"{chat_icon(c)} <b>{escape(c.title or str(c.chat_id))}</b>\n",
             f"Tipo: {kind}" + (f" · @{escape(c.username)}" if c.username else ""),
             f"ID: <code>{c.chat_id}</code>",
             f"O bot é: {status}\n",
             "<b>Permissões do bot</b>"]
    if c.type == ChatType.CHANNEL:
        lines += [f"{yes(c.can_post)} Postar mensagens", f"{yes(c.can_delete)} Apagar mensagens"]
    else:
        lines += [f"{yes(c.can_delete)} Apagar mensagens", f"{yes(c.can_restrict)} Banir usuários",
                  f"{yes(c.can_pin)} Fixar mensagens"]
    miss = missing_rights(c)
    if miss:
        lines.append(f"\n⚠️ Para corrigir: no {kind.lower()} → Administradores → @bot → ligue: {', '.join(miss)}.")
    lines.append("\n• 🚪 Tirar o bot daqui: o bot sai deste " + kind.lower() + ".")
    return "\n".join(lines), kb([cbtn("🔄 Verificar", "chat", "check", c.id)],
                                [cbtn("🚪 Tirar o bot daqui", "chat", "leave", c.id)],
                                [cbtn("« Grupos e canais", "chats")])


async def screen_admins(child: ChildBot, db: Database, is_owner: bool) -> Screen:
    admins = await repo.child_admins(db, child.id)
    lines = ["👮 <b>Admins do bot</b>\n",
             "O dono e os admins podem abrir este painel e colocar o bot em grupos e canais.\n",
             f"Dono: <code>{child.owner_id}</code>", "Admins:"]
    lines += [f"• {escape(a.name or '')} <code>{a.telegram_user_id}</code>" for a in admins] or ["<i>nenhum</i>"]
    if is_owner:
        lines.append("\n<b>O que cada botão faz</b>\n"
                     "• ➕ Adicionar admin: mande o ID da pessoa (ela vê o dela mandando /id aqui no bot).\n"
                     "• ❌ Nome: tira a pessoa dos admins.")
        rows = [[cbtn("➕ Adicionar admin", "admins", "add")]]
        rows += [[cbtn(f"❌ {(a.name or str(a.telegram_user_id))[:30]}", "admins", "del", a.id)] for a in admins]
    else:
        lines.append("\nSó o dono do bot adiciona ou remove admins.")
        rows = []
    return "\n".join(lines), kb(*rows, [cbtn("« Painel", "home")])


def screen_help(child: ChildBot, manager: BotManager) -> Screen:
    text = ("❓ <b>Ajuda</b>\n\n"
            "<b>Colocar o bot num grupo ou canal:</b> 👥 Grupos e canais → ➕ Adicionar. "
            "Ele precisa ser administrador.\n"
            "<b>Outra pessoa da equipe usar o painel:</b> 👮 Admins do bot → ➕ Adicionar admin.\n"
            "<b>Desligar, trocar token ou remover o bot:</b> no "
            f'<a href="{mae_link(manager, "bots")}">bot principal</a>, em 🤖 Meus bots.\n\n'
            "Comandos: /start · /id · /ajuda · /cancelar")
    return text, kb([cbtn("« Painel", "home")])


def public_text(child: ChildBot, manager: BotManager) -> str:
    return (f"🤖 Olá! Eu sou o <b>{escape(child.name)}</b> e ajudo a cuidar de grupos e canais do Telegram.\n\n"
            "Só os administradores usam o meu painel.\n\n"
            f'<i>Quer um bot assim? <a href="{mae_link(manager, f"filho_{child.id}")}">Crie o seu aqui</a>.</i>')


# ---------- comandos no privado ----------

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, state: FSMContext, child: ChildBot, db: Database, manager: BotManager) -> None:
    await state.clear()
    if await repo.is_child_admin(db, child, message.from_user.id):
        await send(message, await screen_home(child, db))
    else:
        await message.answer(public_text(child, manager), link_preview_options={"is_disabled": True})


@router.message(Command("ajuda"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message, child: ChildBot, db: Database, manager: BotManager) -> None:
    if await repo.is_child_admin(db, child, message.from_user.id):
        await send(message, screen_help(child, manager))
    else:
        await message.answer(public_text(child, manager), link_preview_options={"is_disabled": True})


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    text = f"Seu ID: <code>{message.from_user.id}</code>" if message.from_user else ""
    if message.chat.type != ChatType.PRIVATE:
        text += f"\nID deste chat: <code>{message.chat.id}</code>"
    await message.answer(text.strip() or f"ID deste chat: <code>{message.chat.id}</code>")


@router.message(Command("cancelar"), F.chat.type == ChatType.PRIVATE)
async def cancel(message: Message, state: FSMContext, child: ChildBot, db: Database) -> None:
    await state.clear()
    if await repo.is_child_admin(db, child, message.from_user.id):
        await message.answer("Cancelado.")
        await send(message, await screen_home(child, db))


@router.message(StateFilter(ChildFlow.add_admin), F.chat.type == ChatType.PRIVATE)
async def add_admin(message: Message, state: FSMContext, child: ChildBot, db: Database) -> None:
    if message.from_user.id != child.owner_id:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Mande só o número do ID (ex.: <code>123456789</code>) ou /cancelar.")
        return
    uid = int(raw)
    if uid == child.owner_id:
        await message.answer("Esse é você, o dono 🙂 Mande o ID de outra pessoa ou /cancelar.")
        return
    async with db.session() as s:
        exists = await s.scalar(select(ChildAdmin).where(ChildAdmin.child_id == child.id,
                                                         ChildAdmin.telegram_user_id == uid))
        if exists is None:
            s.add(ChildAdmin(child_id=child.id, telegram_user_id=uid))
            await s.commit()
    await state.clear()
    await message.answer(f"✅ <code>{uid}</code> agora é admin do bot. Ela abre o painel mandando /start aqui.")
    await send(message, await screen_admins(child, db, True))


# ---------- botões do painel ----------

@router.callback_query(CP.filter())
async def on_panel(cb: CallbackQuery, callback_data: CP, state: FSMContext, bot: Bot, child: ChildBot,
                   db: Database, manager: BotManager) -> None:
    uid, s_, a, i = cb.from_user.id, callback_data.s, callback_data.a, callback_data.i
    if not await repo.is_child_admin(db, child, uid):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    is_owner = uid == child.owner_id

    if s_ == "home":
        await state.clear()
        await cb.answer()
        await show(cb, await screen_home(child, db))
    elif s_ == "soon":
        await cb.answer()
        await show(cb, screen_soon(a))
    elif s_ == "help":
        await cb.answer()
        await show(cb, screen_help(child, manager))
    elif s_ == "chats":
        if a == "check":
            n = await refresh_rights(bot, db, child)
            await cb.answer(f"Conferi {n} grupo(s)/canal(is).")
        else:
            await cb.answer()
        await show(cb, await screen_chats(child, db))
    elif s_ == "chat":
        async with db.session() as s:
            c = await s.get(ChildChat, i)
        if c is None or c.child_id != child.id:
            await cb.answer("Não encontrado.", show_alert=True)
            return
        if a == "check":
            try:
                member = await bot.get_chat_member(c.chat_id, bot.id)
            except (TelegramBadRequest, TelegramForbiddenError):
                member = None
            async with db.session() as s:
                c = await s.get(ChildChat, i)
                if member is None:
                    c.bot_status = "left"
                else:
                    _apply_member(c, member)
                await s.commit()
            await cb.answer("Conferido.")
        elif a == "leave":
            await cb.answer()
            await show(cb, (f"🚪 <b>Tirar o bot de {escape(c.title)}?</b>\n\nPara voltar, é só adicionar de novo.",
                            kb([cbtn("🚪 Sim, sair", "chat", "leave2", i, style="danger"),
                                cbtn("« Voltar", "chat", i=i)])))
            return
        elif a == "leave2":
            try:
                await bot.leave_chat(c.chat_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            async with db.session() as s:
                row = await s.get(ChildChat, i)
                row.bot_status = "left"
                await s.commit()
            await cb.answer("Saí.")
            await show(cb, await screen_chats(child, db))
            return
        else:
            await cb.answer()
        async with db.session() as s:
            c = await s.get(ChildChat, i)
        if c.bot_status in ("left", "kicked"):
            await show(cb, await screen_chats(child, db))
        else:
            await show(cb, screen_chat(c))
    elif s_ == "admins":
        if a in ("add", "del") and not is_owner:
            await cb.answer("Só o dono do bot.", show_alert=True)
            return
        if a == "add":
            await state.set_state(ChildFlow.add_admin)
            await cb.answer()
            await show(cb, ("➕ <b>Adicionar admin</b>\n\nMande o ID da pessoa (só números). "
                            "Ela vê o dela mandando /id aqui no bot.\n\n/cancelar para desistir.",
                            kb([cbtn("❌ Cancelar", "admins")])))
            return
        if a == "del":
            async with db.session() as s:
                await s.execute(delete(ChildAdmin).where(ChildAdmin.id == i, ChildAdmin.child_id == child.id))
                await s.commit()
            await cb.answer("Removido.")
        else:
            await state.clear()
            await cb.answer()
        await show(cb, await screen_admins(child, db, is_owner))
    else:
        await cb.answer()


# ---------- entrada e saída de grupos/canais ----------

async def _tell_owner(bot: Bot, manager: BotManager, child: ChildBot, text: str) -> None:
    """Avisa o dono pelo próprio bot filho; se ele nunca abriu o bot, avisa pelo bot mãe."""
    for sender in (bot, manager.mother):
        try:
            await sender.send_message(child.owner_id, text, link_preview_options={"is_disabled": True})
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            continue


@router.my_chat_member(F.chat.type != ChatType.PRIVATE)
async def on_my_member(event: ChatMemberUpdated, bot: Bot, child: ChildBot, db: Database,
                       manager: BotManager) -> None:
    chat, new, old, who = event.chat, event.new_chat_member, event.old_chat_member, event.from_user
    title = escape(chat.title or str(chat.id))
    joined = new.status in ("member", "administrator", "creator") and old.status in ("left", "kicked")
    if joined and (who is None or not await repo.is_child_admin(db, child, who.id)):
        try:
            await bot.leave_chat(chat.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        name = escape(who.full_name) if who else "alguém"
        await _tell_owner(bot, manager, child,
                          f"🚫 {name} (<code>{who.id if who else '?'}</code>) tentou colocar o @{escape(child.username)} "
                          f"em <b>{title}</b>. Saí na hora: só você e os admins do bot podem me adicionar.")
        logger.info("@%s saiu de %s: adicionado por %s, que não é admin", child.username, chat.id, who.id if who else None)
        return

    async with db.session() as s:
        row = await s.scalar(select(ChildChat).where(ChildChat.child_id == child.id, ChildChat.chat_id == chat.id))
        if row is None:
            row = ChildChat(child_id=child.id, chat_id=chat.id, type=chat.type)
            s.add(row)
        row.title, row.username, row.type = chat.title or "", chat.username, chat.type
        _apply_member(row, new)
        if joined and who:
            row.added_by = who.id
        await s.commit()
        await s.refresh(row)
        missing = missing_rights(row)

    kind = "canal" if chat.type == ChatType.CHANNEL else "grupo"
    if new.status in ("left", "kicked"):
        text = f"🚪 O @{escape(child.username)} saiu (ou foi removido) do {kind} <b>{title}</b>."
    elif new.status in ("administrator", "creator") and not missing:
        text = f"✅ O @{escape(child.username)} está no {kind} <b>{title}</b> como administrador, com tudo certo."
    else:
        text = (f"⚠️ O @{escape(child.username)} está no {kind} <b>{title}</b>, mas falta: {', '.join(missing)}.\n"
                f"No {kind} → Administradores → @{escape(child.username)} → ligue o que falta.")
    await _tell_owner(bot, manager, child, text)
