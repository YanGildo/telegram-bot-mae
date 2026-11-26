"""Entrada por pedido: o bot aprova ou recusa pedidos de entrada olhando o nome e a bio da pessoa.
Também tem a tela do canal de registros."""

import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatJoinRequest, Message
from sqlalchemy import select

from .. import repo
from ..db import ChildBot, ChildChat, Database, JoinFilter
from ..ui import CP, Screen, cbtn, kb, show
from .flow import ask
from .logs import send_log, who
from .words import normalize

logger = logging.getLogger(__name__)
router = Router(name="entrada")


def _list(raw: str) -> list[str]:
    return [w for w in (raw or "").splitlines() if w.strip()]


def match_any(text: str, words: list[str]) -> str | None:
    norm = normalize(text)
    for w in words:
        nw = normalize(w.strip())
        if nw and nw in norm:
            return w
    return None


def decide(f: JoinFilter, name: str, bio: str, username: str | None) -> tuple[str, str]:
    """Devolve (approve|decline|hold, motivo)."""
    if f.need_username and not username:
        return ("decline" if f.on_block == "decline" else "hold"), "não tem @ (nome de usuário)"
    hit = match_any(name, _list(f.block_words))
    if hit:
        return ("decline" if f.on_block == "decline" else "hold"), f"o nome tem «{hit}»"
    hit = match_any(bio, _list(f.block_words))
    if hit:
        return ("decline" if f.on_block == "decline" else "hold"), f"a bio tem «{hit}»"
    allow = _list(f.allow_words)
    if allow:
        ok = match_any(name + "\n" + bio, allow)
        if not ok:
            return "hold", "nome e bio sem nenhuma palavra da lista de liberados"
        return "approve", f"tem «{ok}»"
    return ("approve" if f.on_clean == "approve" else "hold"), "nome e bio sem palavras bloqueadas"


async def get_filter(db: Database, child_id: int, chat_row_id: int) -> JoinFilter:
    async with db.session() as s:
        f = await s.scalar(select(JoinFilter).where(JoinFilter.child_id == child_id, JoinFilter.chat_row_id == chat_row_id))
        if f is None:
            f = JoinFilter(child_id=child_id, chat_row_id=chat_row_id)
            s.add(f)
            await s.commit()
        return f


# ---------- pedidos de entrada ----------

@router.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot, child: ChildBot, db: Database) -> None:
    async with db.session() as s:
        chat = await s.scalar(select(ChildChat).where(ChildChat.child_id == child.id, ChildChat.chat_id == req.chat.id))
        f = await s.scalar(select(JoinFilter).where(JoinFilter.child_id == child.id,
                                                    JoinFilter.chat_row_id == chat.id)) if chat else None
    if f is None or not f.enabled or (chat is not None and chat.f_join is False):
        return
    user = req.from_user
    verdict, why = decide(f, user.full_name or "", req.bio or "", user.username)
    try:
        if verdict == "approve":
            await req.approve()
        elif verdict == "decline":
            await req.decline()
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await send_log(bot, db, child, f"⚠️ Não consegui responder o pedido de entrada de {who(user)} em "
                                       f"{escape(req.chat.title or '')}: {escape(e.message)}")
        return
    if verdict == "decline":  # só as recusas vão para o registro (aprovações seriam mensagens demais)
        bio = escape((req.bio or "")[:200]) or "<i>sem bio</i>"
        await send_log(bot, db, child, f"🚫 <b>Entrada recusada</b>\n\nQuem: {who(user)}\n"
                                       f"Onde: {escape(req.chat.title or '')}\nBio: {bio}\nMotivo: {escape(why)}")
    logger.info("@%s: pedido de %s em %s → %s (%s)", child.username, user.id, req.chat.id, verdict, why)


# ---------- telas ----------

async def screen_groups(child: ChildBot, db: Database) -> Screen:
    chats = [c for c in await repo.child_chats(db, child.id) if c.bot_status in ("administrator", "creator")]
    lines = ["🚪 <b>Entrada por nome e bio</b>\n",
             "Quando o grupo ou canal pede aprovação para entrar, o bot olha o <b>nome</b> e a <b>bio</b> "
             "de quem pediu e aprova ou recusa sozinho.\n",
             "Para funcionar: no grupo/canal → Convidar por link → crie um link com <b>Pedir aprovação de "
             "administradores</b> ligado. O bot precisa ser administrador com <b>Adicionar membros</b>.\n"]
    rows = []
    async with db.session() as s:
        for c in chats:
            f = await s.scalar(select(JoinFilter).where(JoinFilter.child_id == child.id, JoinFilter.chat_row_id == c.id))
            on = bool(f and f.enabled)
            lines.append(f"{'✅' if on else '❌'} {escape(c.title)}")
            rows.append([cbtn(f"{'✅' if on else '❌'} {c.title[:40]}", "jf", "", c.id)])
    if not chats:
        lines.append("<i>O bot ainda não é administrador em nenhum grupo ou canal.</i>")
    return "\n".join(lines), kb(*rows, [cbtn("« Painel", "home")])


async def screen_filter(child: ChildBot, db: Database, chat: ChildChat) -> Screen:
    f = await get_filter(db, child.id, chat.id)
    block, allow = _list(f.block_words), _list(f.allow_words)
    i = chat.id
    text = (f"🚪 <b>Entrada</b> · {escape(chat.title)}\n\n"
            f"Situação: {'Ligado ✅' if f.enabled else 'Desligado ❌'}\n"
            f"🚫 Palavras bloqueadas (nome ou bio): {len(block)}\n"
            f"✅ Palavras de liberação: {len(allow) or 'nenhuma (todos que passam no bloqueio)'}\n"
            f"Quem tem palavra bloqueada: <b>{'recusar' if f.on_block == 'decline' else 'deixar para você decidir'}</b>\n"
            f"Quem passa: <b>{'aprovar sozinho' if f.on_clean == 'approve' else 'deixar para você decidir'}</b>\n"
            f"Exigir @ (nome de usuário): {'✅' if f.need_username else '❌'}\n\n"
            "<b>Como decide</b>\n"
            "1. Sem @ e com Exigir @ ligado → bloqueado.\n"
            "2. Nome ou bio com palavra bloqueada → bloqueado.\n"
            "3. Se houver palavras de liberação, só aprova quem tiver uma delas no nome ou na bio; "
            "o resto fica esperando você.\n"
            "4. Senão, aprova (ou deixa esperando, se você escolher).\n\n"
            "Ignora acentos e maiúsculas. Só as <b>recusas</b> vão para o 📋 canal de registros.")
    return text, kb([cbtn("⏸ Desligar" if f.enabled else "▶️ Ligar", "jf", "tog", i)],
                    [cbtn("➕ Bloquear palavras", "jf", "addb", i), cbtn("➖ Desbloquear", "jf", "remb", i)],
                    [cbtn("➕ Palavras de liberação", "jf", "adda", i), cbtn("➖ Tirar", "jf", "rema", i)],
                    [cbtn("Bloqueado: " + ("🚫 recusar" if f.on_block == "decline" else "⏳ esperar"), "jf", "onb", i),
                     cbtn("Passou: " + ("✅ aprovar" if f.on_clean == "approve" else "⏳ esperar"), "jf", "onc", i)],
                    [cbtn(f"{'✅' if f.need_username else '❌'} Exigir @", "jf", "user", i),
                     cbtn("🔤 Ver listas", "jf", "list", i)],
                    [cbtn("« Grupos", "joins")])


async def screen_logchat(child: ChildBot, db: Database) -> Screen:
    async with db.session() as s:
        row = await s.get(ChildBot, child.id)
    chats = [c for c in await repo.child_chats(db, child.id) if c.bot_status in ("administrator", "creator")]
    current = next((c.title for c in chats if c.chat_id == row.log_chat_id), None)
    text = ("📋 <b>Canal de registros</b>\n\n"
            "Um canal ou grupo (de preferência só seu e da equipe) onde o bot conta tudo o que fez:\n"
            "• 🔇 silenciados, 🚷 banidos e ❗ expulsos pelas palavras proibidas, com os botões "
            "<b>🔊 Desmutar</b> e <b>♻️ Desbanir</b>\n"
            "• ⚠️ advertências, com <b>🧹 Zerar advertências</b>\n"
            "• 🚪 pedidos de entrada recusados\n"
            "• 🔁 aviso quando o acervo ou uma programação completa a rodada\n\n"
            "Crie o canal, coloque o bot como administrador e escolha abaixo. "
            "Ele não recebe posts das programações.\n\n"
            f"Agora: <b>{escape(current) if current else 'nenhum'}</b>")
    rows = [[cbtn(("✅ " if c.chat_id == row.log_chat_id else "▫️ ") + ("📢 " if c.type == ChatType.CHANNEL else "👥 ")
                  + (c.title or str(c.chat_id))[:40], "logc", "set", c.id)] for c in chats]
    if row.log_chat_id:
        rows.append([cbtn("✖️ Sem canal de registros", "logc", "none")])
    return text, kb(*rows, [cbtn("« Painel", "home")])


@router.callback_query(CP.filter(F.s.in_({"joins", "jf", "logc"})))
async def on_joins(cb: CallbackQuery, callback_data: CP, state: FSMContext, bot: Bot, child: ChildBot,
                   db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    s_, a, i = callback_data.s, callback_data.a, callback_data.i
    await state.clear()
    if s_ == "logc":
        note = None
        if a == "set":
            async with db.session() as s:
                c = await s.get(ChildChat, i)
                row = await s.get(ChildBot, child.id)
                if c and c.child_id == child.id:
                    row.log_chat_id = c.chat_id
                    await s.commit()
            if c:
                try:
                    await bot.send_message(c.chat_id, "📋 Este é o canal de registros do bot. As ações dele aparecem aqui.",
                                           disable_notification=True)
                    note = "Canal de registros definido."
                except (TelegramBadRequest, TelegramForbiddenError) as e:
                    note = f"Salvei, mas não consegui postar lá: {e.message}"
        elif a == "none":
            async with db.session() as s:
                row = await s.get(ChildBot, child.id)
                row.log_chat_id = None
                await s.commit()
            note = "Sem canal de registros."
        await cb.answer(note, show_alert=bool(note and "não consegui" in note))
        return await show(cb, await screen_logchat(child, db))
    if s_ == "joins":
        await cb.answer()
        return await show(cb, await screen_groups(child, db))
    async with db.session() as s:
        chat = await s.get(ChildChat, i)
    if chat is None or chat.child_id != child.id:
        await cb.answer("Não encontrado.", show_alert=True)
        return
    f = await get_filter(db, child.id, i)

    async def save(**fields):
        async with db.session() as s:
            row = await s.get(JoinFilter, f.id)
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    if a == "tog":
        await save(enabled=not f.enabled)
    elif a == "onb":
        await save(on_block="hold" if f.on_block == "decline" else "decline")
    elif a == "onc":
        await save(on_clean="hold" if f.on_clean == "approve" else "approve")
    elif a == "user":
        await save(need_username=not f.need_username)
    elif a in ("addb", "remb", "adda", "rema"):
        what = "bloqueadas" if a.endswith("b") else "de liberação"
        verb = "Adicionar" if a.startswith("add") else "Tirar"
        return await ask(cb, state, f"join_{a}", f"<b>{verb} palavras {what}</b>\n\nUma por linha ou separadas por "
                         "vírgula. Ex.: <code>promo, vip grátis, 18+</code>"
                         + ("\nEnvie <code>*</code> para limpar a lista." if verb == "Tirar" else ""),
                         cbtn("❌ Cancelar", "jf", "", i), chat_row=i)
    elif a == "list":
        body = ("<b>🚫 Bloqueadas</b>\n" + ("\n".join(f"• {escape(w)}" for w in _list(f.block_words)) or "<i>nenhuma</i>")
                + "\n\n<b>✅ Liberação</b>\n" + ("\n".join(f"• {escape(w)}" for w in _list(f.allow_words)) or "<i>nenhuma</i>"))
        await cb.answer()
        return await show(cb, (f"🔤 <b>Listas</b> · {escape(chat.title)}\n\n{body}"[:4000], kb([cbtn("« Voltar", "jf", "", i)])))
    await cb.answer()
    await show(cb, await screen_filter(child, db, chat))


async def handle_input(kind: str, data: dict, message: Message, child: ChildBot, db: Database) -> Screen | str:
    i = data.get("chat_row", 0)
    async with db.session() as s:
        chat = await s.get(ChildChat, i)
    if chat is None or chat.child_id != child.id:
        return "Não encontrado."
    f = await get_filter(db, child.id, i)
    raw = (message.text or "").strip()
    entries = [w.strip() for w in re.split(r"[\n,]+", raw) if w.strip()]
    field = "block_words" if kind.endswith("b") else "allow_words"
    words = _list(getattr(f, field))
    if kind.startswith("join_add"):
        known = {normalize(w) for w in words}
        words += [w for w in entries if normalize(w) not in known]
    elif raw == "*":
        words = []
    else:
        drop = {normalize(w) for w in entries}
        words = [w for w in words if normalize(w) not in drop]
    async with db.session() as s:
        row = await s.get(JoinFilter, f.id)
        setattr(row, field, "\n".join(words))
        await s.commit()
    return await screen_filter(child, db, chat)
