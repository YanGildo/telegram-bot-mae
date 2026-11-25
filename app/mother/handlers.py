"""Bot mãe: criar bots filhos, ver e gerenciar os seus bots, acesso à plataforma."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, select

from .. import repo
from ..config import Settings
from ..db import ChildAdmin, ChildBot, ChildChat, Database
from ..manager import BotManager
from ..security import find_token
from ..ui import MP, Screen, kb, mbtn, send, show, url

logger = logging.getLogger(__name__)
router = Router(name="mae")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class Flow(StatesGroup):
    token = State()  # esperando o token de um bot novo (ou o token novo de um bot existente)
    allow_user = State()  # dono da plataforma liberando alguém
    add_padmin = State()  # dono colocando admin da plataforma


def mae_link(manager: BotManager, param: str) -> str:
    return f"https://t.me/{manager.mother_username}?start={param}"


def status_icon(row: ChildBot, manager: BotManager) -> str:
    if row.status == "invalid":
        return "⚠️"
    if row.status == "stopped":
        return "🔴"
    if row.last_error:
        return "🟡"
    return "🟢" if manager.is_running(row.id) else "🟡"


STATUS_TEXT = {"active": "🟢 Ligado", "stopped": "🔴 Desligado", "invalid": "⚠️ Token não funciona"}


# ---------- telas ----------

async def screen_home(uid: int, name: str, db: Database, settings: Settings, manager: BotManager) -> Screen:
    bots = await repo.bots_of(db, uid)
    text = (f"👋 Olá, <b>{escape(name or 'tudo bem')}</b>!\n\n"
            f"🤖 Eu crio e cuido de bots para os seus grupos e canais do Telegram. "
            f"Cada bot é seu, com o nome que você escolher.\n\n"
            "Com o seu bot você vai poder:\n"
            "• 🗂 postar sozinho um acervo de fotos e vídeos, em ordem ou aleatório\n"
            "• 🔁 programar mensagens repetidas, sem limite\n"
            "• 🚫 bloquear palavras proibidas, com a punição que você escolher\n"
            "• 🤝 fazer parcerias automáticas de divulgação com outros grupos\n\n"
            "Toque em <b>➕ Criar seu bot</b> para começar.\n\n"
            f'🤝 <a href="{mae_link(manager, "parcerias")}">Conheça as parcerias automáticas</a>')
    rows = [[mbtn("➕ Criar seu bot", "new", style="primary")],
            [mbtn(f"🤖 Meus bots ({len(bots)})", "bots")],
            [mbtn("🤝 Parcerias", "parc"), mbtn("❓ Ajuda", "help")]]
    if await repo.is_platform_admin(db, settings, uid):
        rows.append([mbtn("🔐 Acesso e todos os bots", "acc")])
    return text, kb(*rows)


def screen_create() -> Screen:
    text = ("➕ <b>Criar seu bot</b>\n\n"
            "1. Abra o @BotFather (botão abaixo)\n"
            "2. Mande <code>/newbot</code>\n"
            "3. Envie o <b>nome</b> do bot (ex.: Parcerias do Yan)\n"
            "4. Envie o <b>username</b>, que precisa terminar em <i>bot</i> (ex.: parceriasdoyan_bot)\n"
            "5. <b>Encaminhe aqui</b> a mensagem do BotFather que tem o token (ou cole só o token)\n\n"
            "🔒 O token fica guardado criptografado e eu apago a sua mensagem com ele. "
            "Não mande o token para mais ninguém: quem tem o token controla o bot.")
    return text, kb([url("Abrir o @BotFather", "https://t.me/BotFather")], [mbtn("❌ Cancelar", "home", "cancel")])


def screen_closed() -> Screen:
    return ("🔒 <b>Fase fechada</b>\n\nPor enquanto, só pessoas liberadas podem criar bots. "
            "Quando abrir para todos, este botão passa a funcionar.\n\n"
            "Se você recebeu um convite, mande /id e passe o número para quem te convidou."), \
        kb([mbtn("« Início", "home")])


async def screen_bots(uid: int, db: Database, manager: BotManager) -> Screen:
    bots = await repo.bots_of(db, uid)
    if not bots:
        return ("🤖 <b>Meus bots</b>\n\nVocê ainda não tem bots. Toque em ➕ Criar seu bot."), \
            kb([mbtn("➕ Criar seu bot", "new", style="primary")], [mbtn("« Início", "home")])
    lines = ["🤖 <b>Meus bots</b>\n", "🟢 ligado · 🟡 com aviso · 🔴 desligado · ⚠️ token não funciona\n"]
    for b in bots:
        lines.append(f"{status_icon(b, manager)} @{escape(b.username)} — {escape(b.name)}")
    lines.append("\nToque num bot para ligar, desligar, trocar o token ou remover.")
    rows = [[mbtn(f"{status_icon(b, manager)} @{b.username}", "bot", i=b.id)] for b in bots]
    return "\n".join(lines), kb(*rows, [mbtn("➕ Criar outro bot", "new")], [mbtn("« Início", "home")])


async def screen_bot(row: ChildBot, db: Database, manager: BotManager, settings: Settings) -> Screen:
    async with db.session() as s:
        chats = await repo.chat_count(s, row.id)
    status = STATUS_TEXT.get(row.status, row.status)
    if row.status == "active" and not manager.is_running(row.id):
        status = "🟡 Ligando…"
    text = (f"🤖 <b>@{escape(row.username)}</b> — {escape(row.name)}\n\n"
            f"Situação: {status}\n"
            f"Grupos e canais: {chats}\n"
            f"Criado em: {row.created_at.strftime('%d/%m/%Y')}\n"
            + (f"Aviso: {escape(row.last_error)}\n" if row.last_error else "")
            + "\n<b>O que cada botão faz</b>\n"
            "• ⚙️ Abrir o painel: as configurações do bot ficam dentro dele.\n"
            "• ⏸ Desligar / ▶️ Ligar: desligado, o bot não responde nem posta nada.\n"
            "• 🔄 Trocar token: use se você revogou o token no @BotFather.\n"
            "• 🗑 Remover: tira o bot daqui. Ele continua existindo no @BotFather.")
    if not settings.is_owner(row.owner_id):
        text += f"\n\nDono: <code>{row.owner_id}</code>"
    toggle = mbtn("▶️ Ligar", "bot", "on", row.id) if row.status != "active" else mbtn("⏸ Desligar", "bot", "off", row.id)
    return text, kb([url("⚙️ Abrir o painel do bot", f"https://t.me/{row.username}?start=painel")],
                    [toggle, mbtn("🔄 Trocar token", "bot", "tok", row.id)],
                    [mbtn("🗑 Remover", "bot", "rm", row.id)],
                    [mbtn("« Meus bots", "bots")])


def screen_parcerias() -> Screen:
    text = ("🤝 <b>Parcerias automáticas</b>\n\n"
            "Troque divulgação com outros grupos e canais sem ficar postando na mão.\n\n"
            "<b>Como funciona</b>\n"
            "1. Você e o parceiro criam um bot aqui, cada um o seu.\n"
            "2. Um manda o convite e o outro aceita o acordo: de quanto em quanto tempo pode postar, "
            "quantos posts por dia, em quais horários e o que pode ir (texto, foto, vídeo, botões).\n"
            "3. Cada um coloca o bot do outro nos grupos combinados.\n"
            "4. Os bots postam sozinhos e o sistema garante o combinado. "
            "Dá para pausar ou encerrar quando quiser.\n\n"
            "<b>Começar:</b> crie seu bot aqui e, no painel dele, toque em 🤝 Parcerias.\n"
            "Todo post de parceria leva uma marca d'água discreta com o link das parcerias automáticas.")
    return text, kb([mbtn("➕ Criar seu bot", "new", style="primary")], [mbtn("« Início", "home")])


async def screen_invite(code: str, uid: int, db: Database, settings: Settings, manager: BotManager,
                        state: FSMContext) -> Screen:
    from ..child.partners import preview_invite
    from ..db import Partnership
    async with db.session() as s:
        p = await s.scalar(select(Partnership).where(Partnership.code == code.upper()))
    if p is None or p.status != "invited":
        return "Esse convite não está mais valendo. Peça um novo para o parceiro.", kb([mbtn("« Início", "home")])
    a = await repo.get_bot(db, p.a_child_id)
    mine = [b for b in await repo.bots_of(db, uid) if b.id != p.a_child_id and b.status == "active"]
    await state.update_data(invite=p.id)
    text = preview_invite(p, a) + "\n\n"
    if not mine:
        text += ("Para aceitar, você precisa de um bot seu aqui. Toque em ➕ Criar seu bot e depois abra este "
                 "link de novo." if await repo.can_create(db, settings, uid) else
                 "Para aceitar, você precisa de um bot seu aqui, mas a criação ainda está fechada. "
                 "Peça para liberarem você.")
        return text, kb([mbtn("➕ Criar seu bot", "new", style="primary")], [mbtn("« Início", "home")])
    text += "Escolha com qual dos seus bots aceitar:"
    rows = [[mbtn(f"✅ Aceitar com @{b.username}", "pcok", i=b.id)] for b in mine]
    return text, kb(*rows, [mbtn("❌ Recusar", "home")])


def screen_help() -> Screen:
    text = ("❓ <b>Ajuda</b>\n\n"
            "<b>Criar um bot:</b> toque em ➕ Criar seu bot e siga os passos com o @BotFather.\n"
            "<b>Configurar:</b> abra o seu bot e mande /start. O painel dele fica lá.\n"
            "<b>Grupos e canais:</b> no painel do seu bot, em 👥 Grupos e canais, use os botões de adicionar: "
            "o Telegram já abre com as permissões certas.\n"
            "<b>Token vazou?</b> No @BotFather mande /revoke, escolha o bot e depois use 🔄 Trocar token aqui.\n\n"
            "Comandos: /start · /id · /cancelar")
    return text, kb([mbtn("« Início", "home")])


async def screen_access(db: Database, manager: BotManager, is_super: bool = False) -> Screen:
    open_ = await repo.is_open(db)
    people = await repo.allowed_accounts(db)
    all_bots = await repo.all_bots(db)
    lines = ["🔐 <b>Acesso à plataforma</b>\n",
             "Situação: " + ("🔓 <b>Aberta</b>: qualquer pessoa cria bots (até "
                             f"{repo.MAX_BOTS_PER_USER} por pessoa)." if open_ else
                             "🔒 <b>Fechada</b>: só você e as pessoas liberadas criam bots."),
             "\n<b>Liberados</b>"]
    lines += [f"• {escape(p.first_name or '')} <code>{p.telegram_user_id}</code>" for p in people] or ["<i>ninguém ainda</i>"]
    lines.append("\n<b>O que cada botão faz</b>\n"
                 "• ➕ Liberar pessoa: mande o ID dela (ela vê o dela mandando /id aqui).\n"
                 "• ❌ Nome: tira a liberação (os bots que ela já criou continuam).\n"
                 "• 🔓 Abrir / 🔒 Fechar: libera ou fecha a criação de bots para todo mundo.\n"
                 "• 📋 Todos os bots: vê e gerencia os bots de todo mundo.")
    rows = [[mbtn("➕ Liberar pessoa", "acc", "add")]]
    rows += [[mbtn(f"❌ {(p.first_name or str(p.telegram_user_id))[:30]}", "acc", "del", p.telegram_user_id)] for p in people]
    rows.append([mbtn("🔒 Fechar para todos" if open_ else "🔓 Abrir para todos", "acc", "toggle")])
    rows.append([mbtn(f"📋 Todos os bots ({len(all_bots)})", "all")])
    if is_super:
        rows.append([mbtn("👑 Admins da plataforma", "padm")])
    rows.append([mbtn("« Início", "home")])
    return "\n".join(lines), kb(*rows)


async def screen_padmins(db: Database, settings: Settings) -> Screen:
    admins = await repo.platform_admins(db)
    lines = ["👑 <b>Admins da plataforma</b>\n",
             "Admins têm o mesmo poder que você no bot mãe: veem 🔐 Acesso, liberam pessoas, "
             "abrem ou fecham a plataforma e gerenciam os bots de todo mundo. "
             "Só quem está no <code>ADMIN_IDS</code> do servidor (você) mexe nesta lista.\n",
             "<b>Donos (servidor)</b>"]
    lines += [f"• <code>{x}</code>" for x in sorted(settings.owners)]
    lines.append("\n<b>Admins</b>")
    lines += [f"• {escape(a.first_name or '')} <code>{a.telegram_user_id}</code>" for a in admins] or ["<i>nenhum</i>"]
    lines.append("\n• ➕ Adicionar admin: mande o ID da pessoa (ela vê o dela mandando /id aqui).\n"
                 "• ❌ Nome: tira o admin.")
    rows = [[mbtn("➕ Adicionar admin", "padm", "add")]]
    rows += [[mbtn(f"❌ {(a.first_name or str(a.telegram_user_id))[:30]}", "padm", "del", a.telegram_user_id)]
             for a in admins]
    return "\n".join(lines), kb(*rows, [mbtn("« Acesso", "acc")])


async def screen_all(db: Database, manager: BotManager) -> Screen:
    bots = await repo.all_bots(db)
    lines = ["📋 <b>Todos os bots</b>\n"]
    lines += [f"{status_icon(b, manager)} @{escape(b.username)} · dono <code>{b.owner_id}</code>" for b in bots] \
        or ["<i>Nenhum bot ainda.</i>"]
    rows = [[mbtn(f"{status_icon(b, manager)} @{b.username}", "bot", i=b.id)] for b in bots]
    return "\n".join(lines), kb(*rows, [mbtn("« Acesso", "acc")])


# ---------- comandos ----------

@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext, db: Database, settings: Settings,
                manager: BotManager) -> None:
    await state.clear()
    arg = (command.args or "").strip()
    referrer = arg if arg.startswith(("parc_", "filho_")) else None
    await repo.touch_account(db, message.from_user, referrer)
    if arg.startswith("pc_"):
        await send(message, await screen_invite(arg[3:], message.from_user.id, db, settings, manager, state))
        return
    if arg == "parcerias" or arg.startswith("parc_"):
        await send(message, screen_parcerias())
        return
    await send(message, await screen_home(message.from_user.id, message.from_user.first_name, db, settings, manager))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Seu ID: <code>{message.from_user.id}</code>")


@router.message(Command("cancelar"))
async def cancel(message: Message, state: FSMContext, db: Database, settings: Settings, manager: BotManager) -> None:
    await state.clear()
    await message.answer("Cancelado.")
    await send(message, await screen_home(message.from_user.id, message.from_user.first_name, db, settings, manager))


# ---------- botões ----------

@router.callback_query(MP.filter())
async def on_button(cb: CallbackQuery, callback_data: MP, state: FSMContext, db: Database, settings: Settings,
                    manager: BotManager) -> None:
    uid, s_, a, i = cb.from_user.id, callback_data.s, callback_data.a, callback_data.i
    owner = await repo.is_platform_admin(db, settings, uid)

    if s_ == "home":
        await state.clear()
        await cb.answer("Cancelado." if a == "cancel" else None)
        await show(cb, await screen_home(uid, cb.from_user.first_name, db, settings, manager))
        return
    if s_ == "new":
        await cb.answer()
        if not await repo.can_create(db, settings, uid):
            await show(cb, screen_closed())
            return
        if not owner and len(await repo.bots_of(db, uid)) >= repo.MAX_BOTS_PER_USER:
            await cb.answer(f"Limite de {repo.MAX_BOTS_PER_USER} bots por pessoa.", show_alert=True)
            return
        await state.set_state(Flow.token)
        await state.update_data(child_id=None)
        await show(cb, screen_create())
        return
    if s_ == "bots":
        await cb.answer()
        await show(cb, await screen_bots(uid, db, manager))
        return
    if s_ == "parc":
        await cb.answer()
        await show(cb, screen_parcerias())
        return
    if s_ == "help":
        await cb.answer()
        await show(cb, screen_help())
        return
    if s_ == "pcok":
        from ..child.partners import accept, get_p
        pid = (await state.get_data()).get("invite")
        p = await get_p(db, pid or 0)
        b = await repo.get_bot(db, i)
        if p is None or b is None or b.owner_id != uid and not await repo.is_platform_admin(db, settings, uid):
            await cb.answer("Convite não encontrado.", show_alert=True)
            return
        err = await accept(db, manager, p, b, uid)
        if err:
            await cb.answer(err, show_alert=True)
            return
        await state.clear()
        await cb.answer("Parceria aceita!")
        await show(cb, (f"🤝 <b>Parceria aceita!</b>\n\nAgora, no painel do @{escape(b.username)} → 🤝 Parcerias:\n"
                        "1. 👥 Escolha os seus grupos onde o parceiro vai postar.\n"
                        "2. ➕ Coloque o bot dele nesses grupos.\n"
                        "3. 📣 Monte a sua divulgação e ative.",
                        kb([url(f"⚙️ Abrir @{b.username}", f"https://t.me/{b.username}?start=painel")],
                           [mbtn("« Início", "home")])))
        return

    if s_ == "bot":
        row = await repo.get_bot(db, i)
        if row is None or (row.owner_id != uid and not owner):
            await cb.answer("Bot não encontrado.", show_alert=True)
            return
        if a == "on":
            async with db.session() as s:
                r = await s.get(ChildBot, i)
                r.status, r.last_error = "active", None
                await s.commit()
            ok = await manager.start(await repo.get_bot(db, i))
            await cb.answer("Ligado." if ok else "Não consegui ligar. Veja o aviso na tela.", show_alert=not ok)
        elif a == "off":
            await manager.stop(i)
            async with db.session() as s:
                r = await s.get(ChildBot, i)
                r.status = "stopped"
                await s.commit()
            await cb.answer("Desligado.")
        elif a == "tok":
            await state.set_state(Flow.token)
            await state.update_data(child_id=i)
            await cb.answer()
            await show(cb, (f"🔄 <b>Trocar o token do @{escape(row.username)}</b>\n\n"
                            "1. No @BotFather, mande <code>/revoke</code> e escolha este bot (se ainda não fez).\n"
                            "2. Encaminhe aqui a mensagem com o token novo (ou cole só o token).",
                            kb([url("Abrir o @BotFather", "https://t.me/BotFather")],
                               [mbtn("❌ Cancelar", "bot", i=i)])))
            return
        elif a == "rm":
            await cb.answer()
            await show(cb, (f"🗑 <b>Remover o @{escape(row.username)}?</b>\n\n"
                            "Ele para de funcionar e todas as configurações dele somem daqui. "
                            "O bot continua existindo no @BotFather (lá dá para apagar de vez com /deletebot).",
                            kb([mbtn("🗑 Sim, remover", "bot", "rm2", i, style="danger"),
                                mbtn("« Voltar", "bot", i=i)])))
            return
        elif a == "rm2":
            await manager.stop(i)
            async with db.session() as s:
                await s.execute(delete(ChildAdmin).where(ChildAdmin.child_id == i))
                await s.execute(delete(ChildChat).where(ChildChat.child_id == i))
                await s.execute(delete(ChildBot).where(ChildBot.id == i))
                await s.commit()
            logger.info("Bot filho @%s removido por %s", row.username, uid)
            await cb.answer("Removido.")
            await show(cb, await screen_bots(uid, db, manager))
            return
        else:
            await state.clear()
            await cb.answer()
        await show(cb, await screen_bot(await repo.get_bot(db, i), db, manager, settings))
        return

    # ---- só o dono da plataforma ----
    if s_ == "padm":
        if not settings.is_owner(uid):
            await cb.answer("Só o dono (ADMIN_IDS do servidor).", show_alert=True)
            return
        if a == "add":
            await state.set_state(Flow.add_padmin)
            await cb.answer()
            await show(cb, ("➕ <b>Adicionar admin da plataforma</b>\n\nMande o ID da pessoa (só números). "
                            "Ela descobre o dela mandando /id para mim.\n\n/cancelar para desistir.",
                            kb([mbtn("❌ Cancelar", "padm")])))
            return
        if a == "del":
            await repo.set_platform_admin(db, i, False)
            await cb.answer("Admin removido.")
        else:
            await state.clear()
            await cb.answer()
        await show(cb, await screen_padmins(db, settings))
        return
    if s_ in ("acc", "all") and not owner:
        await cb.answer("Só o dono da plataforma.", show_alert=True)
        return
    if s_ == "acc":
        note = None
        if a == "add":
            await state.set_state(Flow.allow_user)
            await cb.answer()
            await show(cb, ("➕ <b>Liberar pessoa</b>\n\nMande o ID da pessoa (só números). "
                            "Ela descobre o dela mandando /id para mim.\n\n/cancelar para desistir.",
                            kb([mbtn("❌ Cancelar", "acc")])))
            return
        if a == "del":
            await repo.set_allowed(db, i, False)
            note = "Liberação removida."
        elif a == "toggle":
            open_ = await repo.is_open(db)
            await repo.set_setting(db, "open_to_all", "0" if open_ else "1")
            note = "Fechada: só liberados criam bots." if open_ else "Aberta: qualquer pessoa cria bots."
        await state.clear()
        await cb.answer(note)
        await show(cb, await screen_access(db, manager, settings.is_owner(uid)))
        return
    if s_ == "all":
        await cb.answer()
        await show(cb, await screen_all(db, manager))
        return
    await cb.answer()


# ---------- textos digitados ----------

@router.message(StateFilter(Flow.add_padmin))
async def add_padmin(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not settings.is_owner(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Mande só o número do ID (ex.: <code>123456789</code>) ou /cancelar.")
        return
    await repo.set_platform_admin(db, int(raw), True)
    await state.clear()
    await message.answer(f"✅ <code>{raw}</code> agora é admin da plataforma. Ela vê 🔐 Acesso mandando /start.")
    await send(message, await screen_padmins(db, settings))


@router.message(StateFilter(Flow.allow_user))
async def allow_user(message: Message, state: FSMContext, db: Database, settings: Settings,
                     manager: BotManager) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Mande só o número do ID (ex.: <code>123456789</code>) ou /cancelar.")
        return
    await repo.set_allowed(db, int(raw), True)
    await state.clear()
    await message.answer(f"✅ <code>{raw}</code> liberado para criar bots.")
    await send(message, await screen_access(db, manager, settings.is_owner(message.from_user.id)))


@router.message(StateFilter(Flow.token))
async def receive_token(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                        manager: BotManager) -> None:
    token = find_token(message.text or message.caption)
    if token is None:
        await message.answer("Não achei um token nessa mensagem. Encaminhe a mensagem do @BotFather "
                             "que tem o token (ex.: <code>123456:ABC-DEF…</code>) ou /cancelar.")
        return
    try:
        await message.delete()  # tira o token da conversa
    except TelegramBadRequest:
        pass
    data = await state.get_data()
    child_id = data.get("child_id")
    uid = message.from_user.id

    test = manager.make_bot(token)
    try:
        me = await test.get_me()
    except TelegramUnauthorizedError:
        await message.answer("❌ Esse token não funciona. Confira se copiou inteiro ou gere outro no @BotFather.")
        return
    except TelegramNetworkError:
        await message.answer("Não consegui falar com o Telegram agora. Tente de novo em alguns segundos.")
        return
    finally:
        await test.session.close()

    if me.id == bot.id:
        await message.answer("Esse é o meu próprio token 🙂 Crie um bot novo no @BotFather.")
        return
    existing = await repo.bot_by_telegram_id(db, me.id)

    if child_id is not None:  # trocar token de um bot existente
        row = await repo.get_bot(db, child_id)
        if row is None or (row.owner_id != uid and not await repo.is_platform_admin(db, settings, uid)):
            await state.clear()
            return
        if me.id != row.telegram_bot_id:
            await message.answer(f"Esse token é do @{escape(me.username)}, não do @{escape(row.username)}. "
                                 "Mande o token do bot certo ou /cancelar.")
            return
        target = row
    elif existing is not None:
        if existing.owner_id != uid and not await repo.is_platform_admin(db, settings, uid):
            await message.answer("Esse bot já está cadastrado por outra pessoa.")
            return
        target = existing  # mesmo dono cadastrando de novo: só atualiza o token
    else:
        target = None

    async with db.session() as s:
        if target is None:
            row = ChildBot(owner_id=uid, telegram_bot_id=me.id, username=me.username or "", name=me.first_name or "",
                           token_enc=manager.box.seal(token), status="active")
            s.add(row)
        else:
            row = await s.get(ChildBot, target.id)
            row.token_enc, row.status, row.last_error = manager.box.seal(token), "active", None
            row.username, row.name = me.username or row.username, me.first_name or row.name
        await s.commit()
        child_pk = row.id
    await state.clear()
    row = await repo.get_bot(db, child_pk)
    ok = await manager.restart(row)
    logger.info("Bot filho @%s %s por %s", me.username, "criado" if target is None else "com token novo", uid)
    if not ok:
        await message.answer("⚠️ Salvei, mas não consegui ligar o bot. Veja em 🤖 Meus bots.")
        return
    title = "✅ <b>@{u} está ligado!</b>" if target is None else "🔄 <b>Token do @{u} atualizado e bot ligado.</b>"
    await message.answer(
        title.format(u=escape(me.username)) + "\n\n<b>Próximos passos</b>\n"
        f"1. Abra o @{escape(me.username)} e mande /start: o painel dele fica lá.\n"
        "2. No painel, em 👥 Grupos e canais, adicione o bot nos seus grupos e canais como administrador.",
        reply_markup=kb([url(f"⚙️ Abrir @{me.username}", f"https://t.me/{me.username}?start=painel")],
                        [mbtn("🤖 Meus bots", "bots")]))
