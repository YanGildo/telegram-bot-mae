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
from aiogram.methods import (BanChatMember, DeleteMessage, DeleteMessages, DeleteWebhook,  # noqa: E402
                             EditMessageText, GetChatAdministrators, GetChatMember, GetMe, GetUpdates, LeaveChat,
                             PinChatMessage, RestrictChatMember, SendDocument, SendMediaGroup, SendMessage, SendPhoto,
                             SendVideo,
                             SetMyCommands)
from aiogram.types import (CallbackQuery, Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember,  # noqa: E402
                           ChatMemberUpdated, Message, MessageEntity, PhotoSize, Update, User, Video)
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
        if isinstance(method, (SendPhoto, SendVideo, SendDocument)):
            self.n += 1
            return Message(message_id=self.n, date=NOW, chat=Chat(id=method.chat_id, type="supergroup"))
        if isinstance(method, SendMediaGroup):
            out = []
            for _ in method.media:
                self.n += 1
                out.append(Message(message_id=self.n, date=NOW, chat=Chat(id=method.chat_id, type="supergroup")))
            return out
        if isinstance(method, GetChatAdministrators):
            return [admin_member(OWNER)]
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
    calls = await csay(OWNER, "/start")
    check("filho: painel sem 'em breve'", not any("em breve" in m.text for m in texts(calls)))

    # ================= Etapas 2-4 =================
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from app.child import scheduling as sched
    from app.db import AcervoItem, PendingDelete, Schedule, WordFilter
    BR = ZoneInfo("America/Sao_Paulo")
    ANA_G = Chat(id=-100604, type="supergroup", title="Grupo da Ana")
    async with db.session() as s:
        rows = {c.chat_id: c for c in await s.scalars(select(ChildChat).where(ChildChat.child_id == c1.id))}
    chan_row, grp_row = rows[-100602], rows[-100604]

    def photo(fid):
        return [PhotoSize(file_id=fid, file_unique_id=fid + "u", width=10, height=10)]

    async def chan_post(**kw):
        m = Message(message_id=nid(), date=NOW, chat=Chat(id=-100602, type="channel", title="Canal do Yan"), **kw)
        return await feed(child_dp, cbot, channel_post=m)

    # --- acervo
    calls = await cclick(OWNER, CP(s="acv").pack())
    check("acervo: tela abre explicando", any("canal de origem" in m.text and "Ver mídias" in m.text for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="acv", a="src").pack())
    check("acervo: lista canais para origem", any("Canal do Yan" in " ".join(buttons(m)) for m in texts(calls)))
    await chan_post(photo=photo("antes"))
    await cclick(OWNER, CP(s="acv", a="setsrc", i=chan_row.id).pack())
    await chan_post(photo=photo("p1"), caption="Legenda 1")
    await chan_post(video=Video(file_id="v1", file_unique_id="v1u", width=1, height=1, duration=3), media_group_id="g1",
                    caption="Álbum")
    await chan_post(photo=photo("p2"), media_group_id="g1")
    await chan_post(text="só texto")
    async with db.session() as s:
        items = list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id).order_by(AcervoItem.seq)))
    check("acervo: captura foto e álbum (antes da origem não entra; texto ignora)",
          len(items) == 2 and items[0].caption_html == "Legenda 1" and items[1].media.count('"f"') == 2)
    calls = await cclick(OWNER, CP(s="acv").pack())
    check("acervo: contagem no painel", any("Mídias: <b>2</b>" in m.text and "📚 1 álbuns" in m.text for m in texts(calls)))

    # limite rotativo / travado
    from app.db import Acervo as AcvRow
    async with db.session() as s:
        a_ = await s.scalar(select(AcvRow).where(AcvRow.child_id == c1.id))
        a_.max_items = 2
        await s.commit()
    await chan_post(photo=photo("p3"))
    async with db.session() as s:
        act = [x.media for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id,
                                                                          AcervoItem.state == "active"))]
    check("acervo: rotativo tira a mais antiga", len(act) == 2 and not any('"p1"' in x for x in act))
    await cclick(OWNER, CP(s="acv", a="mode", i=1).pack())
    calls = await chan_post(photo=photo("p4"))
    async with db.session() as s:
        n_act = len(list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))))
    check("acervo: travado não aceita e avisa o dono", n_act == 2 and any("cheio" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="acv", a="setlim", i=100).pack())
    await cclick(OWNER, CP(s="acv", a="mode", i=0).pack())

    # legenda e botões
    await cclick(OWNER, CP(s="acv", a="in", i=3).pack())
    await csay(OWNER, "Entre no canal!")
    await cclick(OWNER, CP(s="acv", a="in", i=4).pack())
    calls = await csay(OWNER, "sem link")
    check("acervo: botão inválido pede de novo", any("Não entendi" in m.text for m in texts(calls)))
    calls = await csay(OWNER, "Site - seusite.com | VIP - @meucanal")
    check("acervo: botões salvos", any("Salvo" in m.text for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="acv", a="prev").pack())
    prev = [m for _, m in calls if isinstance(m, (SendPhoto, SendMediaGroup))]
    check("acervo: prévia com legenda + texto universal", prev and "Entre no canal!" in (
        prev[0].caption if isinstance(prev[0], SendPhoto) else prev[0].media[0].caption or ""))

    # programação do acervo
    await cclick(OWNER, CP(s="schs", a="acervo").pack())
    await cclick(OWNER, CP(s="schs", a="add", i=0).pack())
    async with db.session() as s:
        sa = await s.scalar(select(Schedule).where(Schedule.child_id == c1.id, Schedule.kind == "acervo"))
    await cclick(OWNER, CP(s="sch", a="tog", i=sa.id).pack())
    async with db.session() as s:
        check("programação: continua desativada sem destino", not (await s.get(Schedule, sa.id)).enabled)
    calls = await cclick(OWNER, CP(s="sch", a="dst", i=sa.id).pack())
    check("programação: destinos não mostram o canal de origem",
          any("Grupo da Ana" in " ".join(buttons(m)) and "Canal do Yan" not in " ".join(buttons(m)) for m in texts(calls)))
    await cclick(OWNER, CP(s="sch", a="dsttog", i=sa.id, j=grp_row.id).pack())
    await cclick(OWNER, CP(s="sch", a="setint", i=sa.id, j=30).pack())
    await cclick(OWNER, CP(s="sch", a="tog", i=sa.id).pack())
    async with db.session() as s:
        sa = await s.get(Schedule, sa.id)
    check("programação: ativa com próximo envio", sa.enabled and sa.next_run_at is not None)
    session.take()
    await sched.tick(manager, now=sa.next_run_at)
    calls = session.take()
    posts = [m for _, m in calls if isinstance(m, (SendPhoto, SendMediaGroup, SendVideo)) and m.chat_id == -100604]
    check("programação: no horário publica do acervo no grupo", len(posts) == 1)
    check("programação: botões do acervo vão junto", any(
        isinstance(m, (SendPhoto, SendMessage)) and m.reply_markup and m.chat_id == -100604 for _, m in calls))
    await sched.tick(manager, now=sa.next_run_at)
    check("programação: não repete no mesmo horário", not any(isinstance(m, (SendPhoto, SendMediaGroup)) for _, m in session.take()))
    async with db.session() as s:
        used = [x.used_count for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id,
                                                                                AcervoItem.state == "active"))]
    await cclick(OWNER, CP(s="sch", a="now", i=sa.id).pack())
    async with db.session() as s:
        used2 = sorted(x.used_count for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id,
                                                                                       AcervoItem.state == "active")))
    check("acervo aleatório: só repete depois de usar todas", sorted(used) == [0, 1] and used2 == [1, 1])

    # apagar anterior, fixar, exclusão automática
    await cclick(OWNER, CP(s="sch", a="delp", i=sa.id).pack())
    await cclick(OWNER, CP(s="sch", a="pin", i=sa.id).pack())
    await cclick(OWNER, CP(s="sch", a="setauto", i=sa.id, j=5).pack())
    calls = await cclick(OWNER, CP(s="sch", a="now", i=sa.id).pack())
    check("programação: apaga a anterior e fixa", any(isinstance(m, DeleteMessages) for _, m in calls)
          and any(isinstance(m, PinChatMessage) and m.disable_notification for _, m in calls))
    async with db.session() as s:
        pd = await s.scalar(select(PendingDelete).where(PendingDelete.chat_id == -100604))
    await sched.tick(manager, now=pd.due_at + timedelta(seconds=1))
    check("programação: exclusão automática apaga depois do tempo", any(isinstance(m, DeleteMessages) for _, m in session.take()))

    # sequencial
    await cclick(OWNER, CP(s="acv", a="setord", i=1).pack())
    await cclick(OWNER, CP(s="acv", a="end", i=1).pack())
    async with db.session() as s:
        a_ = await s.scalar(select(AcvRow).where(AcvRow.child_id == c1.id))
        a_.cursor = 0
        await s.commit()
    i1, _ = await sched.pick_item(db, c1.id)
    i2, _ = await sched.pick_item(db, c1.id)
    i3, why = await sched.pick_item(db, c1.id)
    check("acervo sequencial: na ordem e para no fim", i1.seq < i2.seq and i3 is None and "fim" in why)

    # pausar / zerar / restaurar
    calls = await csay(OWNER, "/pausar")
    sent, why = await sched.run_schedule(cbot, db, settings, sa.id)
    check("acervo: /pausar segura as programações", sent == 0 and why == "acervo pausado")
    await csay(OWNER, "/retomar")
    await cclick(OWNER, CP(s="acv", a="reset2").pack())
    async with db.session() as s:
        n_act = len(list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))))
    await chan_post(photo=photo("depois"))
    async with db.session() as s:
        act = [x.media for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))]
    check("acervo: zerar deixa só as novas", n_act == 0 and len(act) == 1 and '"depois"' in act[0])
    await cclick(OWNER, CP(s="acv", a="restore").pack())
    async with db.session() as s:
        n_act = len(list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))))
    check("acervo: restaurar traz as arquivadas", n_act == 3)
    calls = await cclick(OWNER, CP(s="acv", a="items").pack())
    check("acervo: ver mídias lista com 🗑", any(any(b.startswith("🗑 #") for b in buttons(m)) for m in texts(calls)))

    # --- mensagens repetidas
    calls = await cclick(OWNER, CP(s="schs", a="msg").pack())
    check("repetidas: tela como o Group Help", any("Mensagens repetidas" in m.text and "➕ Adicionar mensagem" in buttons(m)
                                                   for m in texts(calls)))
    await cclick(OWNER, CP(s="schs", a="add", i=1).pack())
    async with db.session() as s:
        sm = await s.scalar(select(Schedule).where(Schedule.child_id == c1.id, Schedule.kind == "msg"))
    await cclick(OWNER, CP(s="sch", a="dsttog", i=sm.id, j=grp_row.id).pack())
    calls = await cclick(OWNER, CP(s="sch", a="tog", i=sm.id).pack())
    async with db.session() as s:
        check("repetidas: não ativa vazia", not (await s.get(Schedule, sm.id)).enabled)
    calls = await cclick(OWNER, CP(s="msgc", i=sm.id).pack())
    check("repetidas: personalizar mostra texto/mídias/botões", any("📄 Texto ❌" in m.text and "👀 Visualização completa"
                                                                   in buttons(m) for m in texts(calls)))
    await cclick(OWNER, CP(s="msgc", a="text", i=sm.id).pack())
    await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"), from_user=OWNER,
                                               text="Oi grupo!", entities=[MessageEntity(type="bold", offset=0, length=3)]))
    await cclick(OWNER, CP(s="msgc", a="media", i=sm.id).pack())
    calls = await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"),
                                                       from_user=OWNER, photo=photo("rep1")))
    check("repetidas: recebe a mídia", any("Mídia recebida" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="msgc", a="btn", i=sm.id).pack())
    await csay(OWNER, "Regras - t.me/regras")
    calls = await cclick(OWNER, CP(s="msgc", a="full", i=sm.id).pack())
    full = [m for _, m in calls if isinstance(m, SendPhoto) and m.chat_id == 111]
    check("repetidas: visualização completa (negrito, foto e botão)", full and full[0].caption == "<b>Oi </b>grupo!"
          and full[0].reply_markup is not None)
    await cclick(OWNER, CP(s="sch", a="start", i=sm.id).pack())
    await csay(OWNER, "23:23")
    await cclick(OWNER, CP(s="sch", a="win", i=sm.id).pack())
    calls = await csay(OWNER, "08:00-23:00")
    await cclick(OWNER, CP(s="sch", a="tog", i=sm.id).pack())
    calls = await cclick(OWNER, CP(s="sch", i=sm.id).pack())
    check("repetidas: detalhe mostra cronograma, repetição e janela",
          any("Cronograma: 23:23" in m.text and "08:00 às 23:00" in m.text and "Ativado ✅" in m.text for m in texts(calls)))
    async with db.session() as s:
        sm = await s.get(Schedule, sm.id)
    nl = sched.to_local(sm.next_run_at, BR)
    check("repetidas: próximo envio alinhado e dentro da janela", nl.minute == 23 and 8 <= nl.hour < 23)
    await sched.tick(manager, now=sm.next_run_at)
    check("repetidas: publica no horário", any(isinstance(m, SendPhoto) and m.chat_id == -100604 for _, m in session.take()))
    # a cada N mensagens + máximo por dia
    await cclick(OWNER, CP(s="sch", a="msgs", i=sm.id).pack())
    await csay(OWNER, "3")
    async with db.session() as s:
        r = await s.get(Schedule, sm.id)
        r.window_start, r.window_end, r.max_per_day, r.posts_today = 0, 1440, 0, 0
        await s.commit()
    for _ in range(2):
        await csay(ANA, "oi", chat=ANA_G)
    calls = await csay(ANA, "oi", chat=ANA_G)
    check("repetidas: a cada 3 mensagens no grupo", any(isinstance(m, SendPhoto) and m.chat_id == -100604 for _, m in calls))
    await cclick(OWNER, CP(s="sch", a="setmax", i=sm.id, j=1).pack())
    for _ in range(3):
        calls = await csay(ANA, "oi", chat=ANA_G)
    check("repetidas: respeita o máximo por dia", not any(isinstance(m, SendPhoto) for _, m in calls))

    # cálculo de horários
    fake = Schedule(start_time="23:23", interval_min=30, weekdays=127, window_start=480, window_end=1380)
    n1 = sched.compute_next(fake, datetime(2026, 11, 18, 10, 0, tzinfo=BR))
    n2 = sched.compute_next(fake, datetime(2026, 11, 18, 22, 55, tzinfo=BR))
    fake.weekdays = 0b0011111
    n3 = sched.compute_next(fake, datetime(2026, 11, 21, 12, 0, tzinfo=BR))  # sábado
    fake.weekdays, fake.end_date = 127, "2026-11-18"
    n4 = sched.compute_next(fake, datetime(2026, 11, 18, 22, 55, tzinfo=BR))
    check("horários: alinhado, janela, dias úteis e fim do período",
          n1.hour == 10 and n1.minute == 23 and (n2.day, n2.hour, n2.minute) == (19, 8, 23)
          and n3.weekday() == 0 and n4 is None)

    # --- palavras proibidas
    calls = await cclick(OWNER, CP(s="words").pack())
    check("palavras: lista os grupos", any("Grupo da Ana" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="wf", a="act", i=grp_row.id, j=4).pack())
    await cclick(OWNER, CP(s="wf", a="add", i=grp_row.id).pack())
    calls = await csay(OWNER, "promo, chama no pv")
    check("palavras: adiciona várias", any("Palavras na lista: 2" in m.text for m in texts(calls)))
    session.take()
    await csay(OWNER, "promoção aqui", chat=ANA_G)
    calls = session.take()
    check("palavras: dono do bot não é punido", not any(isinstance(m, BanChatMember) for _, m in calls))
    calls = await csay(BIA, "Olha a PROMOÇÃO, chamá no PV", chat=ANA_G)
    check("palavras: apaga e bane (ignora acento/maiúscula)", any(isinstance(m, DeleteMessage) for _, m in calls)
          and any(isinstance(m, BanChatMember) and m.user_id == 333 for _, m in calls))
    await cclick(OWNER, CP(s="wf", a="match", i=grp_row.id).pack())
    calls = await csay(BIA, "promoção", chat=ANA_G)
    check("palavras: busca exata não pega dentro de outra palavra", not any(isinstance(m, BanChatMember) for _, m in calls))
    await cclick(OWNER, CP(s="wf", a="act", i=grp_row.id, j=1).pack())
    await cclick(OWNER, CP(s="wf", a="wl", i=grp_row.id).pack())  # 3 -> 4
    async with db.session() as s:
        f = await s.scalar(select(WordFilter).where(WordFilter.chat_row_id == grp_row.id))
        f.warn_limit = 2
        await s.commit()
    calls = await csay(BIA, "promo", chat=ANA_G)
    check("palavras: advertência 1/2", any("Advertência 1/2" in m.text for m in texts(calls))
          and not any(isinstance(m, RestrictChatMember) for _, m in calls))
    calls = await csay(BIA, "promo", chat=ANA_G)
    check("palavras: na 2ª advertência silencia", any(isinstance(m, RestrictChatMember) for _, m in calls))
    await cclick(OWNER, CP(s="wf", a="rem", i=grp_row.id).pack())
    await csay(OWNER, "promo")
    calls = await csay(BIA, "promo", chat=ANA_G)
    check("palavras: remover da lista", not any(isinstance(m, (RestrictChatMember, BanChatMember)) for _, m in calls))

    # ================= v4 =================
    from app.child.acervo import split_existing_albums
    # mesma mídia não entra duas vezes; arquivo (qualidade original) entra
    from aiogram.types import Document
    async with db.session() as s:
        before = len(list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))))
    await chan_post(photo=photo("depois"))
    await chan_post(document=Document(file_id="d1", file_unique_id="d1u", file_name="foto.jpg", mime_type="image/jpeg"))
    async with db.session() as s:
        acts = [x.media for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))]
    check("v4: mídia repetida não duplica; arquivo original entra", len(acts) == before + 1 and any('"doc"' in x for x in acts))
    async with db.session() as s:
        doc_item = next(x for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id)) if '"doc"' in x.media)
    from app.child.content import load_media as lm, send_post as sp
    session.take()
    await sp(cbot, -100604, "legenda", lm(doc_item.media), None)
    check("v4: arquivo sai como arquivo (sem compressão)", any(type(m).__name__ == "SendDocument" for _, m in session.take()))

    # álbuns soltos
    async with db.session() as s:
        n_alb = sum(1 for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))
                    if len(lm(x.media)) > 1)
    calls = await cclick(OWNER, CP(s="acv", a="alb").pack())
    check("v4: tela de álbuns explica inteiros/soltos", any("Soltos" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="acv", a="setalb", i=1).pack())
    async with db.session() as s:
        acts = list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active")))
    check("v4: soltos quebra os álbuns existentes", n_alb >= 1 and all(len(lm(x.media)) == 1 for x in acts)
          and len({x.seq for x in acts}) == len(acts))
    await chan_post(photo=photo("a1"), media_group_id="g9", caption="Álbum novo")
    await chan_post(photo=photo("a2"), media_group_id="g9")
    async with db.session() as s:
        new = list(await s.scalars(select(AcervoItem).where(AcervoItem.media_group_id == "g9")))
    check("v4: álbum novo vira posts soltos com a mesma legenda", len(new) == 2 and all(x.caption_html == "Álbum novo" for x in new))
    await cclick(OWNER, CP(s="acv", a="setalb", i=0).pack())
    await chan_post(photo=photo("b1"), media_group_id="g10")
    await chan_post(photo=photo("b2"), media_group_id="g10")
    async with db.session() as s:
        new = list(await s.scalars(select(AcervoItem).where(AcervoItem.media_group_id == "g10")))
    check("v4: inteiros volta a agrupar os novos", len(new) == 1 and len(lm(new[0].media)) == 2)

    # retomar sem programação ativa avisa
    async with db.session() as s:
        for x in await s.scalars(select(Schedule).where(Schedule.child_id == c1.id, Schedule.kind == "acervo")):
            x.enabled = False
        await s.commit()
    await cclick(OWNER, CP(s="acv", a="pause").pack())
    calls = await cclick(OWNER, CP(s="acv", a="pause").pack())
    check("v4: retomar sem programação ativa avisa", any("Nenhuma programação ativa" in m.text for m in texts(calls)))

    # sem envio duplicado
    async with db.session() as s:
        x = await s.get(Schedule, sa.id)
        x.enabled, x.next_run_at = True, datetime(2026, 1, 1)
        await s.commit()
    session.take()
    await asyncio.gather(sched.tick(manager, now=datetime(2026, 1, 1, 0, 1)), sched.tick(manager, now=datetime(2026, 1, 1, 0, 1)))
    posts = [m for _, m in session.take() if type(m).__name__ in ("SendPhoto", "SendMediaGroup", "SendDocument", "SendVideo")
             and m.chat_id == -100604]
    check("v4: dois agendadores ao mesmo tempo publicam uma vez só", len(posts) == 1)
    r1, r2 = await asyncio.gather(sched.run_schedule(cbot, db, settings, sa.id, force=True),
                                  sched.run_schedule(cbot, db, settings, sa.id, force=True))
    check("v4: dois cliques em Enviar agora = um envio", sorted([r1[0], r2[0]]) == [0, 1])

    # teste de emoji premium
    await cclick(OWNER, CP(s="chats", a="emoji").pack())
    calls = await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"),
                       from_user=OWNER, text="🔥 oi", entities=[MessageEntity(type="custom_emoji", offset=0, length=2,
                                                                                custom_emoji_id="5")]))
    check("v4: teste de emoji premium gera relatório", any("Teste de emoji premium" in m.text and "Fragment" in m.text
                                                          for m in texts(calls)))

    # ================= v5: registros, entrada, nomes, vários conteúdos, rodada =================
    from aiogram.methods import ApproveChatJoinRequest, DeclineChatJoinRequest
    from aiogram.types import ChatJoinRequest
    from app.child.logs import LG
    from app.db import ScheduleItem
    LOGCH = Chat(id=-100605, type="channel", title="Registros")
    await joined(LOGCH, OWNER, admin_member(BOTS["700000001"], post=True))
    async with db.session() as s:
        log_row = await s.scalar(select(ChildChat).where(ChildChat.child_id == c1.id, ChildChat.chat_id == -100605))
    calls = await cclick(OWNER, CP(s="logc").pack())
    check("v5: tela do canal de registros explica", any("Desmutar" in m.text and "Registros" in " ".join(buttons(m))
                                                        for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="logc", a="set", i=log_row.id).pack())
    check("v5: canal de registros definido e avisado", any(m.chat_id == -100605 for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="sch", a="dst", i=sm.id).pack())
    check("v5: canal de registros não aparece como destino", not any("Registros" in " ".join(buttons(m)) for m in texts(calls)))

    # palavras proibidas → registro com botões
    async with db.session() as s:
        f = await s.scalar(select(WordFilter).where(WordFilter.chat_row_id == grp_row.id))
        f.action, f.words, f.match, f.duration_min = "mute", "spam", "contains", 1440
        await s.commit()
    calls = await csay(BIA, "olha o SPAM", chat=ANA_G)
    logm = [m for m in texts(calls) if m.chat_id == -100605]
    check("v5: silenciado vai para o registro com Desmutar", logm and "Silenciado por 1 dia" in logm[0].text
          and "🔊 Desmutar" in buttons(logm[0]) and "333" in logm[0].text)
    calls = await cclick(BIA, LG(a="unmute", c=-100604, u=333).pack())
    check("v5: só admin do bot desmuta", not any(isinstance(m, RestrictChatMember) for _, m in calls))
    calls = await cclick(OWNER, LG(a="unmute", c=-100604, u=333).pack())
    um = [m for _, m in calls if isinstance(m, RestrictChatMember)]
    check("v5: Desmutar devolve a permissão de falar", um and um[0].permissions.can_send_messages)
    async with db.session() as s:
        f = await s.get(WordFilter, f.id)
        f.action = "ban"
        await s.commit()
    calls = await csay(BIA, "spam", chat=ANA_G)
    logm = [m for m in texts(calls) if m.chat_id == -100605]
    check("v5: banido vai para o registro com Desbanir", logm and "♻️ Desbanir" in buttons(logm[0]))
    calls = await cclick(OWNER, LG(a="unban", c=-100604, u=333).pack())
    check("v5: Desbanir", any(type(m).__name__ == "UnbanChatMember" for _, m in calls))
    async with db.session() as s:
        f = await s.get(WordFilter, f.id)
        f.action = "off"
        await s.commit()

    # entrada por nome e bio
    calls = await cclick(OWNER, CP(s="joins").pack())
    check("v5: tela de entrada explica o link com aprovação", any("Pedir aprovação" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="jf", a="tog", i=grp_row.id).pack())
    await cclick(OWNER, CP(s="jf", a="addb", i=grp_row.id).pack())
    calls = await csay(OWNER, "promo, Vip Grátis")
    check("v5: adiciona palavras bloqueadas", any("Palavras bloqueadas (nome ou bio): 2" in m.text for m in texts(calls)))

    async def join_req(user, bio):
        r = ChatJoinRequest(chat=ANA_G, from_user=user, user_chat_id=user.id, date=NOW, bio=bio)
        return await feed(child_dp, cbot, chat_join_request=r)
    SPAMMY = User(id=901, is_bot=False, first_name="Canal", last_name="PROMOÇÕES")
    calls = await join_req(SPAMMY, "")
    check("v5: nome com palavra bloqueada é recusado e registrado",
          any(isinstance(m, DeclineChatJoinRequest) for _, m in calls)
          and any(m.chat_id == -100605 and "Entrada recusada" in m.text and "nome" in m.text for m in texts(calls)))
    calls = await join_req(User(id=902, is_bot=False, first_name="Carla", username="carla"), "Entre no meu vip grátis")
    check("v5: bio com palavra bloqueada é recusada (ignora acento)", any(isinstance(m, DeclineChatJoinRequest) for _, m in calls))
    calls = await join_req(User(id=903, is_bot=False, first_name="Duda", username="duda"), "Amo games")
    check("v5: quem passa é aprovado, sem mensagem no registro", any(isinstance(m, ApproveChatJoinRequest) for _, m in calls)
          and not any(m.chat_id == -100605 for m in texts(calls)))
    await cclick(OWNER, CP(s="jf", a="user", i=grp_row.id).pack())
    calls = await join_req(User(id=904, is_bot=False, first_name="Sem Arroba"), "")
    check("v5: exigir @ recusa quem não tem", any(isinstance(m, DeclineChatJoinRequest) for _, m in calls))
    await cclick(OWNER, CP(s="jf", a="adda", i=grp_row.id).pack())
    await csay(OWNER, "games")
    calls = await join_req(User(id=905, is_bot=False, first_name="Eva", username="eva"), "Viajante")
    check("v5: com lista de liberação, quem não tem a palavra fica esperando",
          not any(isinstance(m, (ApproveChatJoinRequest, DeclineChatJoinRequest)) for _, m in calls)
          and not any(m.chat_id == -100605 for m in texts(calls)))
    await cclick(OWNER, CP(s="jf", a="tog", i=grp_row.id).pack())
    calls = await join_req(User(id=906, is_bot=False, first_name="Promo"), "")
    check("v5: entrada desligada não mexe nos pedidos", not any(isinstance(m, (ApproveChatJoinRequest, DeclineChatJoinRequest))
                                                                for _, m in calls))

    # nome da programação
    await cclick(OWNER, CP(s="sch", a="name", i=sm.id).pack())
    await csay(OWNER, "Divulgação VIP noite")
    calls = await cclick(OWNER, CP(s="schs", a="msg").pack())
    check("v5: programação com nome na lista", any("Divulgação VIP noite" in m.text for m in texts(calls)))

    # vários conteúdos
    calls = await cclick(OWNER, CP(s="msgc", i=sm.id).pack())
    check("v5: personalizar tem Vários conteúdos", any("📸 Mídias (1)" in buttons(m) for m in texts(calls)))
    await cclick(OWNER, CP(s="msgi", a="add", i=sm.id).pack())
    await csay(OWNER, "Conteúdo A")
    await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"), from_user=OWNER,
                                               photo=photo("cB"), caption="Conteúdo B"))
    await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"), from_user=OWNER,
                                               photo=photo("cC1"), media_group_id="alb5", caption="Conteúdo C"))
    calls = await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"),
                                                       from_user=OWNER, photo=photo("cC2"), media_group_id="alb5"))
    async with db.session() as s:
        its = list(await s.scalars(select(ScheduleItem).where(ScheduleItem.schedule_id == sm.id).order_by(ScheduleItem.seq)))
    check("v5: cada mensagem/álbum vira um conteúdo", len(its) == 3 and its[2].media.count('"f"') == 2)
    calls = await cclick(OWNER, CP(s="msgi", i=sm.id).pack())
    check("v5: tela lista os conteúdos", any("Conteúdo A" in m.text and "álbum com 2" in m.text for m in texts(calls)))
    sent_order = []
    for _ in range(3):
        calls = await cclick(OWNER, CP(s="sch", a="now", i=sm.id).pack())
        for _, m in calls:
            if getattr(m, "chat_id", None) == -100604:
                if isinstance(m, SendMessage) and m.text.startswith("Conteúdo"):
                    sent_order.append(m.text)
                elif isinstance(m, SendPhoto):
                    sent_order.append(m.caption)
                elif isinstance(m, SendMediaGroup):
                    sent_order.append(m.media[0].caption)
    check("v5: usa um conteúdo por envio, em ordem", sent_order == ["Conteúdo A", "Conteúdo B", "Conteúdo C"])
    check("v5: aviso de rodada completa no registro", any(m.chat_id == -100605 and "Rodada completa" in m.text for m in texts(calls)))
    async with db.session() as s:
        n_acv = len(list(await s.scalars(select(AcervoItem).where(AcervoItem.media.contains('"cB"')))))
    check("v5: conteúdos não entram no acervo", n_acv == 0)

    # rodada do acervo (aleatório) avisa
    await cclick(OWNER, CP(s="acv", a="setord", i=0).pack())
    async with db.session() as s:
        for x in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id)):
            x.used_count = 0
        await s.commit()
        n_items = len(list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == c1.id, AcervoItem.state == "active"))))
    notes = 0
    for _ in range(n_items):
        calls = await cclick(OWNER, CP(s="sch", a="now", i=sa.id).pack())
        notes += sum(1 for m in texts(calls) if "Rodada completa" in m.text and "acervo" in m.text)
    check("v5: acervo aleatório avisa uma vez ao fechar a rodada", notes == 1)

    # ================= v6: ban em todos + anti-spam no bot filho =================
    from aiogram.methods import UnbanChatMember
    from aiogram.types import ChatMemberBanned
    from app.child.moderation import BA
    G7 = Chat(id=-100606, type="supergroup", title="Grupo 7")
    await joined(G7, OWNER, admin_member(BOTS["700000001"]))
    session.take()

    async def ban_in(chat, by, target):
        ev = ChatMemberUpdated(chat=chat, from_user=by, date=NOW, old_chat_member=ChatMemberMember(user=target),
                               new_chat_member=ChatMemberBanned(user=target, until_date=0))
        return await feed(child_dp, cbot, chat_member=ev)

    calls = await cclick(OWNER, CP(s="ban").pack())
    check("v6: tela Ban em todos explica e mostra comandos", any("/banirtodos" in m.text and "Importar" in " ".join(buttons(m))
                                                                 for m in texts(calls)))
    VIL = User(id=4001, is_bot=False, first_name="Vilão")
    calls = await ban_in(ANA_G, OWNER, VIL)
    bans = {m.chat_id for _, m in calls if isinstance(m, BanChatMember) and m.user_id == 4001}
    check("v6: ban do dono espalha (sem o canal de registros)", bans == {-100602, -100606})
    check("v6: registro com Desbanir de todos", any(m.chat_id == -100605 and "Ban em todos" in m.text
                                                    and "♻️ Desbanir de todos" in buttons(m) for m in texts(calls)))
    calls = await ban_in(ANA_G, BIA, User(id=4002, is_bot=False, first_name="X"))
    check("v6: ban de quem não é admin do bot não espalha", not any(isinstance(m, BanChatMember) for _, m in calls))
    GH = User(id=999, is_bot=True, first_name="Group Help")
    calls = await ban_in(ANA_G, GH, User(id=4003, is_bot=False, first_name="Y"))
    check("v6: ban do Group Help não espalha", not any(isinstance(m, BanChatMember) for _, m in calls))
    ANONU = User(id=1087968824, is_bot=True, first_name="Group")
    calls = await ban_in(ANA_G, ANONU, User(id=4004, is_bot=False, first_name="Z"))
    check("v6: ban de admin anônimo espalha", any(isinstance(m, BanChatMember) and m.user_id == 4004 for _, m in calls))
    calls = await ban_in(ANA_G, OWNER, ANA)
    check("v6: admin do bot nunca é banido em todos", not any(isinstance(m, BanChatMember) for _, m in calls))
    calls = await csay(OWNER, "/banirtodos 4005")
    check("v6: /banirtodos ID no privado", {m.chat_id for _, m in calls if isinstance(m, BanChatMember) and m.user_id == 4005}
          == {-100602, -100604, -100606} and any(m.chat_id == 111 and "4005" in m.text for m in texts(calls)))
    calls = await csay(BIA, "/banirtodos 4006")
    check("v6: /banirtodos só para admins do bot", not any(isinstance(m, BanChatMember) for _, m in calls))
    tgt = Message(message_id=nid(), date=NOW, chat=ANA_G, from_user=User(id=4007, is_bot=False, first_name="W"), text="oi")
    cmd = Message(message_id=nid(), date=NOW, chat=ANA_G, from_user=OWNER, text="/banirtodos",
                  entities=[MessageEntity(type="bot_command", offset=0, length=11)], reply_to_message=tgt)
    calls = await feed(child_dp, cbot, message=cmd)
    check("v6: respondendo no grupo: comando some e bane em todos",
          any(isinstance(m, DeleteMessage) and m.chat_id == -100604 for _, m in calls)
          and {-100602, -100604, -100606} <= {m.chat_id for _, m in calls if isinstance(m, BanChatMember) and m.user_id == 4007}
          and not any(isinstance(m, SendMessage) and m.chat_id == -100604 for _, m in calls))
    calls = await cclick(OWNER, BA(u=4001).pack())
    check("v6: Desbanir de todos", {m.chat_id for _, m in calls if isinstance(m, UnbanChatMember) and m.user_id == 4001}
          >= {-100602, -100604, -100606})
    calls = await csay(OWNER, "/desbanirtodos 4005")
    check("v6: /desbanirtodos", any(isinstance(m, UnbanChatMember) and m.user_id == 4005 for _, m in calls))
    G8 = Chat(id=-100607, type="supergroup", title="Grupo 8")
    calls = await joined(G8, OWNER, admin_member(BOTS["700000001"]))
    check("v6: grupo novo recebe os bans ativos", {m.user_id for _, m in calls if isinstance(m, BanChatMember)
                                                   and m.chat_id == -100607} >= {4004, 4007})
    await cclick(OWNER, CP(s="ban", a="imp").pack())
    calls = await csay(OWNER, "5001, 5002\n5003")
    check("v6: importar IDs bane cada um", {m.user_id for _, m in calls if isinstance(m, BanChatMember)} >= {5001, 5002, 5003})
    await cclick(OWNER, CP(s="ban", a="tog").pack())
    calls = await ban_in(ANA_G, OWNER, User(id=4008, is_bot=False, first_name="Q"))
    check("v6: desligado não espalha", not any(isinstance(m, BanChatMember) for _, m in calls))
    await cclick(OWNER, CP(s="ban", a="tog").pack())

    # anti-spam
    await cclick(OWNER, CP(s="spam", a="add").pack())
    calls = await csay(OWNER, "@ListaDivulga https://t.me/outrocanal")
    check("v6: anti-spam adiciona @ e link", any("@listadivulga" in m.text and "@outrocanal" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="spam", a="delay", i=0).pack())
    LISTA = User(id=4100, is_bot=False, first_name="Lista", username="ListaDivulga")
    calls = await csay(LISTA, "Entre na lista", chat=ANA_G)
    check("v6: apaga na hora mensagem do @ da lista", any(isinstance(m, DeleteMessage) and m.chat_id == -100604 for _, m in calls))
    calls = await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=ANA_G, from_user=BIA,
                       text="vem pro @outrocanal", entities=[MessageEntity(type="mention", offset=8, length=11)]))
    check("v6: apaga mensagem que divulga o @", any(isinstance(m, DeleteMessage) for _, m in calls))
    calls = await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=ANA_G, from_user=OWNER,
                       text="vem pro @outrocanal", entities=[MessageEntity(type="mention", offset=8, length=11)]))
    check("v6: admin do bot não tem mensagem apagada", not any(isinstance(m, DeleteMessage) for _, m in calls))
    await cclick(OWNER, CP(s="spam", a="delay", i=60).pack())
    calls = await csay(LISTA, "de novo", chat=ANA_G)
    check("v6: com espera, não apaga na hora", not any(isinstance(m, DeleteMessage) for _, m in calls))
    await sched.tick(manager, now=datetime(2030, 1, 1))
    check("v6: apaga depois da espera", any(isinstance(m, DeleteMessages) and m.chat_id == -100604 for _, m in session.take()))
    calls = await cclick(OWNER, CP(s="spam", a="del", i=0).pack())
    calls = await csay(LISTA, "e agora", chat=ANA_G)
    check("v6: tirou da lista, não apaga mais", not any(isinstance(m, (DeleteMessage,)) for _, m in calls))

    # ================= v8: variações de texto =================
    await cclick(OWNER, CP(s="msgi", a="clr", i=sm.id).pack())
    await cclick(OWNER, CP(s="msgc", a="text", i=sm.id).pack())
    await csay(OWNER, "Copy 1")
    calls = await cclick(OWNER, CP(s="msgc", i=sm.id).pack())
    check("v8: personalizar tem Variações de texto", any("📄 Texto (1)" in buttons(m) for m in texts(calls)))
    for t in ("Copy 2", "Copy 3"):
        await cclick(OWNER, CP(s="msgv", a="add", i=sm.id).pack())
        calls = await csay(OWNER, t)
    check("v8: lista as variações", any("<b>3.</b> Copy 3" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="msgi", a="add", i=sm.id).pack())
    for fid in ("m1", "m2"):
        await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"),
                                                   from_user=OWNER, photo=photo(fid)))
    await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=Chat(id=111, type="private"),
                                               from_user=OWNER, photo=photo("prova"), caption="Prova social!"))
    async with db.session() as s:
        r = await s.get(Schedule, sm.id)
        r.max_per_day, r.content_order, r.content_cursor, r.text_cursor = 0, "seq", 0, 0
        await s.commit()
    caps = []
    for _ in range(6):
        calls = await cclick(OWNER, CP(s="sch", a="now", i=sm.id).pack())
        caps += [m.caption for _, m in calls if isinstance(m, SendPhoto) and m.chat_id == -100604]
    check("v8: mídias sem texto revezam as copies; a com texto próprio sai sempre com o dela",
          caps == ["Copy 1", "Copy 2", "Prova social!", "Copy 3", "Copy 1", "Prova social!"])
    async with db.session() as s:
        it1 = await s.scalar(select(ScheduleItem).where(ScheduleItem.schedule_id == sm.id).order_by(ScheduleItem.seq))
    await cclick(OWNER, CP(s="msgi", a="txt", i=sm.id, j=it1.id).pack())
    calls = await csay(OWNER, "Texto fixo do 1")
    check("v8: dá texto próprio a um conteúdo", any("💬 Texto fixo do 1" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="msgv", a="rm", i=sm.id, j=0).pack())
    calls = await cclick(OWNER, CP(s="msgv", i=sm.id).pack())
    check("v8: tirar variação", any("Copy 2" not in m.text and "<b>2.</b> Copy 3" in m.text for m in texts(calls)))

    calls = await cclick(OWNER, CP(s="msgc", a="full", i=sm.id).pack())
    check("v11: visualização completa mostra um conteúdo da lista", any(isinstance(m, SendPhoto) and m.chat_id == 111 for _, m in calls))
    calls = await cclick(OWNER, CP(s="msgc", i=sm.id).pack())
    check("v11: tela indica que as mídias vêm do 📚", any("em rodízio" in m.text for m in texts(calls)))

    # ================= v10: funções por grupo, textos sorteados um por um =================
    calls = await cclick(OWNER, CP(s="chat", i=grp_row.id).pack())
    check("v10: detalhe do grupo mostra as funções ligadas", any("✅ 📣 Publicar" in buttons(m) and "✅ 🔨 Ban em todos" in buttons(m)
                                                                  for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="chat", a="feat", i=grp_row.id, j=0).pack())
    check("v10: desliga Publicar só nesse grupo", any("❌ 📣 Publicar" in buttons(m) for m in texts(calls)))
    async with db.session() as s:
        r = await s.get(Schedule, sm.id)
        r.max_per_day, r.last_run_at = 0, None
        await s.commit()
    calls = await cclick(OWNER, CP(s="sch", a="now", i=sm.id).pack())
    check("v10: com Publicar desligado nada sai no grupo", not any(getattr(m, "chat_id", None) == -100604 and
                                                                  type(m).__name__ in ("SendPhoto", "SendMessage", "SendMediaGroup")
                                                                  for _, m in calls))
    calls = await cclick(OWNER, CP(s="sch", a="dst", i=sm.id).pack())
    check("v10: destinos marcam 🚫 o grupo sem publicação", any("🚫" in " ".join(buttons(m)) for m in texts(calls)))
    await cclick(OWNER, CP(s="chat", a="feat", i=grp_row.id, j=0).pack())
    await cclick(OWNER, CP(s="chat", a="feat", i=grp_row.id, j=3).pack())  # ban em todos desligado aqui
    calls = await csay(OWNER, "/banirtodos 6001")
    check("v10: grupo com Ban em todos desligado fica de fora", not any(isinstance(m, BanChatMember) and m.chat_id == -100604
                                                                      for _, m in calls)
          and any(isinstance(m, BanChatMember) and m.user_id == 6001 for _, m in calls))
    await cclick(OWNER, CP(s="chat", a="feat", i=grp_row.id, j=3).pack())
    await cclick(OWNER, CP(s="acv", a="rnd").pack())
    for t in ("Sorte A", "Sorte B"):
        await cclick(OWNER, CP(s="acv", a="in", i=2).pack())
        calls = await csay(OWNER, t)
    check("v10: textos sorteados adicionados um por um", any("<b>2.</b> Sorte B" in m.text for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="acv", a="rndrm", i=0).pack())
    check("v10: tirar texto sorteado", any("<b>1.</b> Sorte B" in m.text for m in texts(calls)))

    # ================= v7: parcerias =================
    from app.db import Partnership, PartnerChat, ScheduleDest, ChildAdmin
    from app.child import scheduling as sch_mod
    async with db.session() as s:  # Ana deixa de ser admin do bot do Yan: entra só como parceira
        for x in await s.scalars(select(ChildAdmin).where(ChildAdmin.child_id == c1.id, ChildAdmin.telegram_user_id == 222)):
            await s.delete(x)
        await s.commit()
    sch_mod.MOTHER_USERNAME = "mae_bot"
    c2bot = manager.bot_for(c2.id)
    AG2 = Chat(id=-100710, type="supergroup", title="Grupo da Ana 2")

    async def c2click(user, data):
        return await click(user, data, bot=c2bot, dp=child_dp)

    async def c2say(user, text, chat=None):
        return await say(user, text, bot=c2bot, dp=child_dp, chat=chat)

    async def join_as(bot_obj, bot_user, chat, by, member):
        ev = ChatMemberUpdated(chat=chat, from_user=by, date=NOW, old_chat_member=ChatMemberLeft(user=bot_user),
                               new_chat_member=member)
        return await feed(child_dp, bot_obj, my_chat_member=ev)

    await join_as(c2bot, BOTS["700000002"], AG2, ANA, admin_member(BOTS["700000002"]))
    async with db.session() as s:
        ag2_row = await s.scalar(select(ChildChat).where(ChildChat.child_id == c2.id, ChildChat.chat_id == -100710))
        g7_row = await s.scalar(select(ChildChat).where(ChildChat.child_id == c1.id, ChildChat.chat_id == -100606))

    calls = await cclick(OWNER, CP(s="parc").pack())
    check("parc: tela explica e tem Nova parceria", any("➕ Nova parceria" in buttons(m) and "acordo" in m.text for m in texts(calls)))
    await cclick(OWNER, CP(s="parc", a="new").pack())
    async with db.session() as s:
        p1 = await s.scalar(select(Partnership).where(Partnership.a_child_id == c1.id))
    await cclick(OWNER, CP(s="ptt", a="setint", i=p1.id, j=30).pack())
    await cclick(OWNER, CP(s="ptt", a="setmax", i=p1.id, j=3).pack())
    await cclick(OWNER, CP(s="ptt", a="btns", i=p1.id).pack())
    calls = await cclick(OWNER, CP(s="ptt", a="invite", i=p1.id).pack())
    check("parc: convite exige escolher grupos antes", any("Escolha antes" in (m.text or "") for _, m in calls
                                                           if type(m).__name__ == "AnswerCallbackQuery"))
    await cclick(OWNER, CP(s="ptg", a="tog", i=p1.id, j=g7_row.id).pack())
    calls = await cclick(OWNER, CP(s="ptt", a="invite", i=p1.id).pack())
    p1 = await __import__("app.child.partners", fromlist=["x"]).get_p(db, p1.id)
    check("parc: convite gerado com link do bot mãe e código", p1.status == "invited" and any(
        f"start=pc_{p1.code}" in m.text and "30 minutos" in m.text and "Até 3" in m.text for m in texts(calls)))

    calls = await say(ANA, f"/start pc_{p1.code}")
    check("parc: link do convite mostra o acordo e os bots da pessoa", any(
        "Convite de parceria" in m.text and "✅ Aceitar com @filho2_bot" in buttons(m) for m in texts(calls)))
    calls = await click(ANA, MP(s="pcok", i=c2.id).pack())
    p1 = await __import__("app.child.partners", fromlist=["x"]).get_p(db, p1.id)
    check("parc: aceitar ativa e avisa quem convidou", p1.status == "active" and p1.b_child_id == c2.id
          and any(m.chat_id == 111 and "Parceria aceita" in m.text for m in texts(calls)))
    async with db.session() as s:
        schs_p = list(await s.scalars(select(Schedule).where(Schedule.partnership_id == p1.id)))
    check("parc: cada lado ganha a sua divulgação (desligada)", len(schs_p) == 2 and not any(x.enabled for x in schs_p))
    sch_yan = next(x for x in schs_p if x.child_id == c1.id)

    await c2click(ANA, CP(s="ptg", a="tog", i=p1.id, j=ag2_row.id).pack())
    calls = await join_as(cbot, BOTS["700000001"], AG2, ANA, ChatMemberMember(user=BOTS["700000001"]))
    check("parc: parceiro coloca meu bot no grupo dele e ele fica", not any(isinstance(m, LeaveChat) for _, m in calls))
    calls = await join_as(c2bot, BOTS["700000002"], G7, OWNER, ChatMemberMember(user=BOTS["700000002"]))
    check("parc: eu coloco o bot dele no meu grupo liberado", not any(isinstance(m, LeaveChat) for _, m in calls))
    calls = await join_as(cbot, BOTS["700000001"], Chat(id=-100711, type="supergroup", title="Outro da Ana"), ANA,
                          ChatMemberMember(user=BOTS["700000001"]))
    check("parc: grupo NÃO liberado continua bloqueado", any(isinstance(m, LeaveChat) for _, m in calls))
    async with db.session() as s:
        dests = list(await s.scalars(select(ScheduleDest).where(ScheduleDest.schedule_id == sch_yan.id)))
    check("parc: destinos da minha divulgação = grupos liberados dele", len(dests) == 1)
    calls = await cclick(OWNER, CP(s="chats").pack())
    check("parc: grupo do parceiro não aparece nos meus grupos", not any("Grupo da Ana 2" in m.text for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="pt", i=p1.id).pack())
    check("parc: painel mostra grupos dos dois lados com ✅", any("✅ Grupo 7" in m.text and "✅ Grupo da Ana 2" in m.text
                                                                  for m in texts(calls)))

    # divulgação
    calls = await cclick(OWNER, CP(s="sch", a="trig", i=sch_yan.id).pack())
    check("parc: repetição não oferece menos que o acordo", any(not ({"5 min", "10 min", "15 min", "20 min"} & set(buttons(m)))
                                                                and "✅ 30 min" in buttons(m) and "Pela parceria" in m.text
                                                                for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="sch", a="setmax", i=sch_yan.id, j=10).pack())
    check("parc: máximo por dia não passa do acordo", any("até 3 por dia" in (m.text or "") for _, m in calls
                                                          if type(m).__name__ == "AnswerCallbackQuery"))
    await cclick(OWNER, CP(s="msgc", a="text", i=sch_yan.id).pack())
    await csay(OWNER, "Conheça meu grupo!")
    await cclick(OWNER, CP(s="msgc", a="btn", i=sch_yan.id).pack())
    await csay(OWNER, "Entrar - t.me/meugrupo")
    calls = await cclick(OWNER, CP(s="sch", a="now", i=sch_yan.id).pack())
    post = [m for _, m in calls if isinstance(m, SendMessage) and m.chat_id == -100710]
    check("parc: posta no grupo do parceiro com a marca d'água", post and "Conheça meu grupo!" in post[0].text
          and "start=parc_" in post[0].text and "Parcerias automáticas" in post[0].text)
    check("parc: acordo sem botões tira os botões", post and post[0].reply_markup is None)
    calls = await cclick(OWNER, CP(s="sch", a="now", i=sch_yan.id).pack())
    check("parc: não passa do intervalo mínimo combinado", not any(isinstance(m, SendMessage) and m.chat_id == -100710
                                                                   for _, m in calls)
          and any("no mínimo 30 minutos" in (m.text or "") for m in texts(calls)))
    calls = await cclick(OWNER, CP(s="sch", a="del", i=sch_yan.id).pack())
    check("parc: divulgação da parceria não se exclui", any("não se exclui" in (m.text or "") for _, m in calls
                                                             if type(m).__name__ == "AnswerCallbackQuery"))

    # grupo do parceiro não é moderado pelo meu bot
    calls = await feed(child_dp, cbot, message=Message(message_id=nid(), date=NOW, chat=AG2, from_user=BIA,
                       text="vem pro @outrocanal", entities=[MessageEntity(type="mention", offset=8, length=11)]))
    check("parc: meu bot não modera o grupo do parceiro", not any(isinstance(m, (DeleteMessage, BanChatMember)) for _, m in calls))

    # pausar / retomar
    await c2click(ANA, CP(s="ptt", a="pause", i=p1.id).pack())
    async with db.session() as s:
        r = await s.get(Schedule, sch_yan.id)
        r.last_run_at = None
        await s.commit()
    sent, why = await sch_mod.run_schedule(cbot, db, settings, sch_yan.id, force=True)
    check("parc: pausada ninguém posta", sent == 0 and "não está ativa" in why)
    calls = await cclick(OWNER, CP(s="ptt", a="resume", i=p1.id).pack())
    check("parc: só quem pausou retoma", any("só ele retoma" in (m.text or "") for _, m in calls
                                             if type(m).__name__ == "AnswerCallbackQuery"))
    await c2click(ANA, CP(s="ptt", a="resume", i=p1.id).pack())
    sent, why = await sch_mod.run_schedule(cbot, db, settings, sch_yan.id, force=True)
    check("parc: retomada volta a postar", sent == 1)

    # convite por código e parceria duplicada
    await c2click(ANA, CP(s="parc", a="new").pack())
    async with db.session() as s:
        p2 = await s.scalar(select(Partnership).where(Partnership.a_child_id == c2.id))
    await c2click(ANA, CP(s="ptg", a="tog", i=p2.id, j=ag2_row.id).pack())
    await c2click(ANA, CP(s="ptt", a="invite", i=p2.id).pack())
    p2 = await __import__("app.child.partners", fromlist=["x"]).get_p(db, p2.id)
    await cclick(OWNER, CP(s="parc", a="code").pack())
    calls = await csay(OWNER, p2.code.lower())
    check("parc: não deixa duas parcerias ativas entre os mesmos bots", any("já têm uma parceria" in m.text for m in texts(calls)))
    await csay(OWNER, "/cancelar")

    # encerrar
    await cclick(OWNER, CP(s="ptt", a="end1", i=p1.id).pack())
    calls = await cclick(OWNER, CP(s="ptt", a="end2", i=p1.id).pack())
    left = {(b, m.chat_id) for b, m in calls if isinstance(m, LeaveChat)}
    check("parc: encerrar tira o bot de cada um dos grupos do outro", (700000001, -100710) in left and (700000002, -100606) in left)
    check("parc: encerrar avisa o parceiro", any(m.chat_id == 222 and "encerrada" in m.text for m in texts(calls)))
    sent, why = await sch_mod.run_schedule(cbot, db, settings, sch_yan.id, force=True)
    check("parc: encerrada não posta mais", sent == 0)
    calls = await say(BIA, "/start parcerias")
    check("parc: tela do bot mãe sem 'chegando'", any("Parcerias automáticas" in m.text and "chegando" not in m.text
                                                      for m in texts(calls)))

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
