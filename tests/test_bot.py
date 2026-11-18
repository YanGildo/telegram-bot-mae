"""Testa o bot mãe e os bots filhos com um Telegram falso (nada é enviado de verdade).

Uso: python tests/test_bot.py
"""

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.exceptions import TelegramUnauthorizedError  # noqa: E402
from aiogram.methods import (DeleteMessage, DeleteWebhook, EditMessageText, GetChatMember, GetMe,  # noqa: E402
                             GetUpdates, LeaveChat, SendMessage, SetMyCommands)
from aiogram.types import (CallbackQuery, Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember,  # noqa: E402
                           ChatMemberUpdated, Message, Update, User)
from cryptography.fernet import Fernet  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.__main__ import build_child_dispatcher, build_mother_dispatcher  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Account, ChildBot, ChildChat, Database  # noqa: E402
from app.manager import BotManager  # noqa: E402
from app.ui import CP, MP  # noqa: E402

NOW = datetime(2026, 11, 18, 12, 0)
MOTHER_TOKEN = "900000001:" + "M" * 35
TOK1 = "700000001:" + "A" * 35
TOK1B = "700000001:" + "B" * 35
TOK2 = "700000002:" + "C" * 35
BAD = "700000099:" + "Z" * 35
BOTS = {
    "900000001": User(id=900000001, is_bot=True, first_name="Mãe", username="mae_bot"),
    "700000001": User(id=700000001, is_bot=True, first_name="Parcerias do Yan", username="filho1_bot"),
    "700000002": User(id=700000002, is_bot=True, first_name="Outro", username="filho2_bot"),
}
OWNER = User(id=111, is_bot=False, first_name="Yan", username="yan")
ANA = User(id=222, is_bot=False, first_name="Ana")
BIA = User(id=333, is_bot=False, first_name="Bia")
GROUP = Chat(id=-100601, type="supergroup", title="Grupo do Yan")
CHAN = Chat(id=-100602, type="channel", title="Canal do Yan")


def admin_member(user, restrict=True, post=False):
    return ChatMemberAdministrator(
        user=user, can_be_edited=False, is_anonymous=False, can_manage_chat=True, can_delete_messages=True,
        can_manage_video_chats=False, can_restrict_members=restrict, can_promote_members=False,
        can_change_info=False, can_invite_users=True, can_post_stories=False, can_edit_stories=False,
        can_delete_stories=False, can_post_messages=post, can_pin_messages=True,
        can_send_welcome_messages=False)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls, self.n = [], 1000
        self.restrict = True

    async def make_request(self, bot, method, timeout=None):
        key = bot.token.split(":")[0]
        if isinstance(method, GetUpdates):
            await asyncio.sleep(0.02)
            return []
        self.calls.append((bot.id, method))
        if bot.token not in (MOTHER_TOKEN, TOK1, TOK1B, TOK2):
            raise TelegramUnauthorizedError(method=method, message="Unauthorized")
        if isinstance(method, GetMe):
            return BOTS[key]
        if isinstance(method, (SendMessage, EditMessageText)):
            self.n += 1
            return Message(message_id=self.n, date=NOW, chat=Chat(id=method.chat_id or 1, type="private"),
                           text=method.text)
        if isinstance(method, GetChatMember):
            return admin_member(BOTS[key], restrict=self.restrict)
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass

    def take(self):
        c, self.calls = self.calls, []
        return c


results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("OK      " if cond else "FALHOU  ") + name)


def texts(calls, bot_id=None):
    return [m for b, m in calls if isinstance(m, (SendMessage, EditMessageText)) and (bot_id is None or b == bot_id)]


def buttons(m):
    return [b.text for row in (m.reply_markup.inline_keyboard if m.reply_markup else []) for b in row]


async def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    settings = Settings(_env_file=None, bot_token=MOTHER_TOKEN, admin_ids="111",
                        secret_key=SecretStr(Fernet.generate_key().decode()))
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    session = FakeSession()
    mother = Bot(MOTHER_TOKEN, session=session)
    child_dp = build_child_dispatcher(db, settings)
    manager = BotManager(db, settings, child_dp, mother, session_factory=lambda: session, poll_timeout=1)
    manager.mother_username = "mae_bot"
    child_dp["manager"] = manager
    mother_dp = build_mother_dispatcher(db, settings, manager)
    n = [0]

    def nid():
        n[0] += 1
        return n[0]

    async def feed(dp, bot, **kw):
        await dp.feed_update(bot, Update(update_id=nid(), **kw))
        await asyncio.sleep(0)
        return session.take()

    def msg(user, text, chat=None):
        return Message(message_id=nid(), date=NOW, chat=chat or Chat(id=user.id, type="private"), from_user=user,
                       text=text)

    async def say(user, text, bot=None, dp=None, chat=None):
        return await feed(dp or mother_dp, bot or mother, message=msg(user, text, chat))

    async def click(user, data, bot=None, dp=None):
        cb = CallbackQuery(id=str(nid()), from_user=user, chat_instance="x", data=data,
                           message=Message(message_id=5, date=NOW, chat=Chat(id=user.id, type="private"), text="x"))
        return await feed(dp or mother_dp, bot or mother, callback_query=cb)

    # ---------- bot mãe ----------
    calls = await say(OWNER, "/start")
    home = texts(calls)
    check("mãe: /start mostra Criar seu bot e Meus bots", home and "➕ Criar seu bot" in buttons(home[0])
          and "🤖 Meus bots (0)" in buttons(home[0]))
    check("mãe: dono vê Acesso", "🔐 Acesso e todos os bots" in buttons(home[0]))
    check("mãe: última linha leva às parcerias", 'href="https://t.me/mae_bot?start=parcerias"' in home[0].text)

    await say(ANA, "/start")
    calls = await click(ANA, MP(s="new").pack())
    check("mãe: fase fechada bloqueia quem não foi liberado", any("Fase fechada" in m.text for m in texts(calls)))
    calls = await say(ANA, "/start")
    check("mãe: quem não é dono não vê Acesso", "🔐 Acesso e todos os bots" not in buttons(texts(calls)[0]))
    calls = await click(ANA, MP(s="acc").pack())
    check("mãe: só dono abre Acesso", not any("Acesso à plataforma" in m.text for m in texts(calls)))

    calls = await click(OWNER, MP(s="new").pack())
    check("mãe: criar mostra o passo a passo do BotFather", any("/newbot" in m.text for m in texts(calls)))
    calls = await say(OWNER, "oi")
    check("mãe: mensagem sem token pede de novo", any("Não achei um token" in m.text for m in texts(calls)))
    calls = await say(OWNER, f"Use this token to access the HTTP API:\n{BAD}")
    check("mãe: token inválido avisa", any("não funciona" in m.text for m in texts(calls)))
    calls = await say(OWNER, f"Done! Congratulations on your new bot.\nUse this token to access the HTTP API:\n{TOK1}\nKeep it safe")
    check("mãe: apaga a mensagem com o token", any(isinstance(m, DeleteMessage) for _, m in calls))
    check("mãe: bot filho criado e avisado", any("@filho1_bot está ligado" in m.text for m in texts(calls)))
    check("mãe: filho recebe comandos e sem webhook",
          any(b == 700000001 and isinstance(m, SetMyCommands) for b, m in calls)
          and any(b == 700000001 and isinstance(m, DeleteWebhook) for b, m in calls))
    async with db.session() as s:
        c1 = await s.scalar(select(ChildBot).where(ChildBot.telegram_bot_id == 700000001))
    check("mãe: token guardado criptografado", c1 and TOK1 not in c1.token_enc and manager.box.open(c1.token_enc) == TOK1)
    await asyncio.sleep(0.05)
    check("mãe: filho rodando", manager.is_running(c1.id))

    await click(OWNER, MP(s="new").pack())
    calls = await say(OWNER, MOTHER_TOKEN)
    check("mãe: recusa o token da própria mãe", any("meu próprio token" in m.text for m in texts(calls)))
    await say(OWNER, "/cancelar")

    # liberar Ana
    calls = await click(OWNER, MP(s="acc").pack())
    check("mãe: tela de acesso fechada", any("Fechada" in m.text for m in texts(calls)))
    await click(OWNER, MP(s="acc", a="add").pack())
    calls = await say(OWNER, "222")
    check("mãe: libera pessoa pelo ID", any("liberado" in m.text for m in texts(calls)))
    await click(ANA, MP(s="new").pack())
    calls = await say(ANA, TOK1)
    check("mãe: bot de outra pessoa não pode ser recadastrado", any("outra pessoa" in m.text for m in texts(calls)))
    calls = await say(ANA, TOK2)
    check("mãe: pessoa liberada cria bot", any("@filho2_bot está ligado" in m.text for m in texts(calls)))
    async with db.session() as s:
        c2 = await s.scalar(select(ChildBot).where(ChildBot.telegram_bot_id == 700000002))
    calls = await click(ANA, MP(s="bot", i=c1.id).pack())
    check("mãe: não abre bot dos outros", not any("Situação" in m.text for m in texts(calls)))
    calls = await click(OWNER, MP(s="all").pack())
    check("mãe: dono vê todos os bots", any("@filho1_bot" in m.text and "@filho2_bot" in m.text for m in texts(calls)))
    await click(OWNER, MP(s="acc", a="toggle").pack())
    await say(BIA, "/start")
    calls = await click(BIA, MP(s="new").pack())
    check("mãe: aberta para todos libera qualquer um", any("/newbot" in m.text for m in texts(calls)))
    await say(BIA, "/cancelar")
    await click(OWNER, MP(s="acc", a="toggle").pack())
    await click(OWNER, MP(s="acc", a="del", i=222).pack())
    check("mãe: tirar liberação", not await __import__("app.repo", fromlist=["x"]).can_create(db, settings, 222))

    # admins da plataforma (colocados pelo dono dentro do bot)
    calls = await click(OWNER, MP(s="acc").pack())
    check("padm: dono vê o botão Admins da plataforma", any("👑 Admins da plataforma" in buttons(m) for m in texts(calls)))
    calls = await click(BIA, MP(s="padm", a="add").pack())
    check("padm: quem não é dono não mexe", not any("Adicionar admin da plataforma" in m.text for m in texts(calls)))
    await click(OWNER, MP(s="padm", a="add").pack())
    calls = await say(OWNER, "333")
    check("padm: dono adiciona admin pelo ID", any("admin da plataforma" in m.text for m in texts(calls)))
    calls = await say(BIA, "/start")
    check("padm: admin vê Acesso", "🔐 Acesso e todos os bots" in buttons(texts(calls)[0]))
    calls = await click(BIA, MP(s="acc").pack())
    e = texts(calls)
    check("padm: admin abre Acesso, mas sem a lista de admins", e and "Acesso à plataforma" in e[-1].text
          and "👑 Admins da plataforma" not in buttons(e[-1]))
    calls = await click(BIA, MP(s="new").pack())
    check("padm: admin cria bots mesmo com a plataforma fechada", any("/newbot" in m.text for m in texts(calls)))
    await say(BIA, "/cancelar")
    await click(OWNER, MP(s="padm", a="del", i=333).pack())
    calls = await say(BIA, "/start")
    check("padm: dono tira o admin", "🔐 Acesso e todos os bots" not in buttons(texts(calls)[0]))

    # meus bots / ligar e desligar / trocar token
    calls = await click(OWNER, MP(s="bots").pack())
    check("mãe: meus bots lista com status", any("🟢 @filho1_bot" in m.text for m in texts(calls)))
    calls = await click(OWNER, MP(s="bot", i=c1.id).pack())
    e = texts(calls)
    check("mãe: tela do bot explica os botões", e and "Trocar token" in e[-1].text and "⏸ Desligar" in buttons(e[-1]))
    await click(OWNER, MP(s="bot", a="off", i=c1.id).pack())
    check("mãe: desligar para o bot", not manager.is_running(c1.id))
    async with db.session() as s:
        check("mãe: desligado fica salvo", (await s.get(ChildBot, c1.id)).status == "stopped")
    await click(OWNER, MP(s="bot", a="on", i=c1.id).pack())
    await asyncio.sleep(0.05)
    check("mãe: ligar de novo", manager.is_running(c1.id))
    await click(OWNER, MP(s="bot", a="tok", i=c1.id).pack())
    calls = await say(OWNER, TOK2)
    check("mãe: trocar token recusa token de outro bot", any("não do @filho1_bot" in m.text for m in texts(calls)))
    calls = await say(OWNER, TOK1B)
    async with db.session() as s:
        r = await s.get(ChildBot, c1.id)
    check("mãe: trocar token salva o novo e religa", any("Token do @filho1_bot atualizado" in m.text for m in texts(calls))
          and manager.box.open(r.token_enc) == TOK1B)
    await asyncio.sleep(0.05)
    child1 = manager.bot_for(c1.id)
    check("mãe: filho usa o token novo", child1 is not None and child1.token == TOK1B)

    # parcerias
    calls = await say(BIA, "/start parc_7")
    check("mãe: link de parceria abre a tela de parcerias", any("Parcerias automáticas" in m.text for m in texts(calls)))
    async with db.session() as s:
        acc = await s.scalar(select(Account).where(Account.telegram_user_id == 333))
    check("mãe: guarda de onde a pessoa veio", acc.referrer == "parc_7")

    # ---------- bot filho ----------
    cbot = manager.bot_for(c1.id)

    async def csay(user, text, chat=None):
        return await say(user, text, bot=cbot, dp=child_dp, chat=chat)

    async def cclick(user, data):
        return await click(user, data, bot=cbot, dp=child_dp)

    calls = await csay(OWNER, "/start")
    e = texts(calls)
    check("filho: dono abre o painel", e and "Painel do @filho1_bot" in e[0].text and "👥 Grupos e canais (0)" in buttons(e[0]))
    calls = await csay(BIA, "/start")
    e = texts(calls)
    check("filho: estranho vê texto público com link da mãe", e and "start=filho_" in e[0].text and "Painel" not in e[0].text)
    calls = await cclick(BIA, CP(s="home").pack())
    check("filho: estranho não usa o painel", not texts(calls))

    async def joined(chat, by, member):
        ev = ChatMemberUpdated(chat=chat, from_user=by, date=NOW, old_chat_member=ChatMemberLeft(user=BOTS["700000001"]),
                               new_chat_member=member)
        return await feed(child_dp, cbot, my_chat_member=ev)

    calls = await joined(GROUP, OWNER, admin_member(BOTS["700000001"]))
    check("filho: entrou no grupo como admin e avisou o dono",
          any(m.chat_id == 111 and "com tudo certo" in m.text for m in texts(calls)))
    calls = await joined(CHAN, OWNER, admin_member(BOTS["700000001"], restrict=False, post=False))
    check("filho: canal sem permissão de postar avisa o que falta",
          any("falta: postar mensagens" in m.text for m in texts(calls)))
    calls = await joined(Chat(id=-100603, type="supergroup", title="Grupo estranho"), BIA,
                         ChatMemberMember(user=BOTS["700000001"]))
    check("filho: colocado por estranho sai na hora", any(isinstance(m, LeaveChat) for _, m in calls)
          and any("tentou colocar" in m.text for m in texts(calls)))
    async with db.session() as s:
        chats = list(await s.scalars(select(ChildChat).where(ChildChat.child_id == c1.id)))
    check("filho: registra só os grupos certos", sorted(c.chat_id for c in chats) == [-100602, -100601])

    calls = await cclick(OWNER, CP(s="chats").pack())
    e = texts(calls)
    check("filho: tela de grupos com ✅ e ⚠️ e botões de adicionar",
          e and "✅ Grupo do Yan" in e[-1].text and "⚠️ Canal do Yan" in e[-1].text
          and "➕ Adicionar a um grupo" in buttons(e[-1]))
    add_group = [b.url for row in e[-1].reply_markup.inline_keyboard for b in row if b.url and "startgroup" in b.url]
    check("filho: link de adicionar já pede as permissões", add_group and "delete_messages" in add_group[0]
          and "restrict_members" in add_group[0])
    session.restrict = False
    calls = await cclick(OWNER, CP(s="chats", a="check").pack())
    check("filho: verificar permissões consulta o Telegram",
          sum(1 for _, m in calls if isinstance(m, GetChatMember)) == 2
          and any("falta: banir usuários" in m.text for m in texts(calls)))
    session.restrict = True
    gid = next(c.id for c in chats if c.chat_id == -100601)
    calls = await cclick(OWNER, CP(s="chat", i=gid).pack())
    check("filho: detalhe do grupo mostra permissões", any("Apagar mensagens" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="chat", a="leave", i=gid).pack())
    calls = await cclick(OWNER, CP(s="chat", a="leave2", i=gid).pack())
    check("filho: tirar o bot do grupo", any(isinstance(m, LeaveChat) and m.chat_id == -100601 for _, m in calls))

    # admins do bot
    await cclick(OWNER, CP(s="admins", a="add").pack())
    calls = await csay(OWNER, "222")
    check("filho: dono adiciona admin", any("agora é admin" in m.text for m in texts(calls)))
    calls = await csay(ANA, "/start")
    check("filho: admin abre o painel", any("Painel do @filho1_bot" in m.text for m in texts(calls)))
    calls = await cclick(ANA, CP(s="admins", a="add").pack())
    check("filho: admin não adiciona outros admins", not any("Adicionar admin</b>" in m.text for m in texts(calls)))
    calls = await joined(Chat(id=-100604, type="supergroup", title="Grupo da Ana"), ANA, admin_member(BOTS["700000001"]))
    check("filho: admin do bot pode colocar o bot em grupo", not any(isinstance(m, LeaveChat) for _, m in calls))
    calls = await csay(ANA, "/id", chat=Chat(id=-100604, type="supergroup", title="Grupo da Ana"))
    check("filho: /id no grupo mostra o ID do chat", any("-100604" in m.text for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="soon", a="acervo").pack())
    check("filho: telas futuras explicam a função", any("Acervo" in m.text and "próxima etapa" in m.text for m in texts(calls)))

    # ---------- reinício e token revogado ----------
    await manager.stop_all()
    manager2 = BotManager(db, settings, child_dp, mother, session_factory=lambda: session, poll_timeout=1)
    n_on = await manager2.start_all()
    check("reinício: liga de novo os bots ativos", n_on == 2)
    await manager2.stop_all()
    async with db.session() as s:
        r = await s.get(ChildBot, c2.id)
        r.token_enc = manager.box.seal(BAD)
        await s.commit()
    session.take()
    ok = await manager2.start(await __import__("app.repo", fromlist=["x"]).get_bot(db, c2.id))
    calls = session.take()
    async with db.session() as s:
        r = await s.get(ChildBot, c2.id)
    check("token revogado: marca inválido e avisa o dono pelo bot mãe",
          not ok and r.status == "invalid" and any(m.chat_id == 222 and "não funciona mais" in m.text for m in texts(calls)))
    calls = await click(ANA, MP(s="bots").pack())
    check("token revogado: aparece ⚠️ em Meus bots", any("⚠️ @filho2_bot" in m.text for m in texts(calls)))

    # remover
    await click(OWNER, MP(s="bot", a="rm", i=c1.id).pack())
    await click(OWNER, MP(s="bot", a="rm2", i=c1.id).pack())
    async with db.session() as s:
        gone = await s.get(ChildBot, c1.id)
        left = list(await s.scalars(select(ChildChat).where(ChildChat.child_id == c1.id)))
    check("remover: apaga o bot e os grupos dele", gone is None and not left and not manager.is_running(c1.id))

    await manager.stop_all()
    await db.close()
    ok = sum(1 for _, r in results if r)
    print(f"\n{ok}/{len(results)} passaram")
    sys.exit(0 if ok == len(results) else 1)


asyncio.run(main())
