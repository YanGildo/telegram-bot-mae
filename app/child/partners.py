"""Parcerias: dois bots filhos combinam um acordo e cada um divulga nos grupos do outro.

Como funciona (v13)
1. A cria uma PARCERIA (um modelo: ex. «1000 membros»): acordo, limites, requisitos, os grupos dele onde
   os parceiros vão postar e a divulgação dele (texto, mídias, links, botões). Gera o convite.
2. O mesmo convite serve para várias pessoas. Cada uma que aceita vira um PARCEIRO dentro dessa parceria,
   com uma cópia do acordo. Para outro acordo (ex.: «2000 membros»), crie outra parceria.
3. Cada parceiro coloca o bot de A nos grupos dele, e A coloca o bot do parceiro nos grupos liberados.
4. A divulgação de A vai para os grupos de todos os parceiros ativos; cada parceiro monta a dele.
   O sistema não deixa passar do acordo, e todo post de parceria leva a marca d'água do bot mãe.
Parcerias antigas (um convite = um parceiro) continuam funcionando como antes.
"""

import logging
import re
import secrets
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy import or_, select

from .. import repo
from ..db import ChildBot, ChildChat, Database, PartnerChat, Partnership, Schedule, ScheduleDest, utcnow
from ..ui import CP, Screen, cbtn, kb, show, url
from .content import plain_len
from .flow import ask
from .scheduling import hhmm, interval_text, next_text, parse_date, parse_hhmm, reschedule

logger = logging.getLogger(__name__)
router = Router(name="parcerias")

INTERVALS = [15, 30, 60, 120, 180, 360, 720, 1440]
MAXES = [1, 2, 3, 5, 10, 24, 48]
CHARS = [300, 500, 800, 1000]
LINES = [5, 10, 15, 20]
STATUS = {"draft": "📝 rascunho", "invited": "📨 convite aberto", "closed": "🔒 convite fechado",
          "active": "🟢 ativa", "paused": "⏸ pausada", "ended": "❌ encerrada"}
GROUP_ADD = "startgroup=parceria"
CHANNEL_ADD = "startchannel=true&admin=post_messages+edit_messages+delete_messages"
TERMS = ("min_interval", "max_day", "window_start", "window_end", "allow_media", "allow_buttons", "end_date",
         "max_chars", "max_lines", "req_open", "req_members", "req_note", "name")


def new_code() -> str:
    return secrets.token_hex(4).upper()


def other_id(p: Partnership, me: int) -> int | None:
    return p.b_child_id if p.a_child_id == me else p.a_child_id


def is_offer(p: Partnership | None) -> bool:
    """Modelo de parceria (convite reaproveitável), não um par de bots."""
    return p is not None and p.offer_id is None and p.b_child_id is None


def pname(p: Partnership) -> str:
    return p.name or f"Parceria {p.id}"


def max_text(n: int) -> str:
    return "sem limite" if not n else f"até {n}"


def terms_text(p: Partnership) -> str:
    window = "dia todo" if p.window_start == 0 and p.window_end >= 1440 else \
        f"{hhmm(p.window_start)} às {hhmm(p.window_end)}"
    content = "texto" + (", foto/vídeo" if p.allow_media else "") + (", botões" if p.allow_buttons else "")
    end = "sem data" if not p.end_date else "até " + "/".join(reversed(p.end_date.split("-")))
    limits = []
    if p.max_chars:
        limits.append(f"até {p.max_chars} caracteres")
    if p.max_lines:
        limits.append(f"até {p.max_lines} linhas")
    return (f"⏱ No mínimo {interval_text(p.min_interval)} entre posts\n"
            f"🔢 {'Sem limite de posts por dia' if not p.max_day else f'Até {p.max_day} post(s) por dia em cada grupo'}\n"
            f"⏰ Horário: {window}\n"
            f"📝 Pode ir: {content}\n"
            f"✂️ Tamanho do texto: {', '.join(limits) if limits else 'sem limite'}\n"
            f"📆 Duração: {end}\n"
            "🤝 Todo post leva a marca d'água das parcerias automáticas")


def req_text(p: Partnership) -> str:
    out = []
    if p.req_open:
        out.append("💬 Chat aberto (membros conversam no grupo)")
    if p.req_members:
        out.append(f"👥 No mínimo {p.req_members} membros")
    if p.req_note:
        out.append(f"📝 {escape(p.req_note)}")
    return "\n".join(out) or "nenhum"


def limits_error(p, text_html: str) -> str:
    """O texto passa do tamanho combinado? (vale para os dois lados)"""
    if p is None:
        return ""
    plain = re.sub(r"<[^>]+>", "", text_html or "")
    if getattr(p, "max_chars", 0) and plain_len(text_html) > p.max_chars:
        return f"o texto tem {plain_len(text_html)} caracteres e o acordo é de até {p.max_chars}"
    lines = plain.strip().count("\n") + 1 if plain.strip() else 0
    if getattr(p, "max_lines", 0) and lines > p.max_lines:
        return f"o texto tem {lines} linhas e o acordo é de até {p.max_lines}"
    return ""


def ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    mins = int((utcnow() - dt).total_seconds() // 60)
    if mins < 60:
        return f"há {max(mins, 1)} min"
    if mins < 1440:
        return f"há {mins // 60} h"
    return f"há {mins // 1440} dia(s)"


async def get_p(db: Database, pid: int) -> Partnership | None:
    async with db.session() as s:
        return await s.get(Partnership, pid)


async def bot_row(db: Database, child_id: int | None) -> ChildBot | None:
    if child_id is None:
        return None
    async with db.session() as s:
        return await s.get(ChildBot, child_id)


async def members(db: Database, offer: Partnership, live_only: bool = False) -> list[Partnership]:
    """Os parceiros que entraram numa parceria (modelo)."""
    sts = ("active",) if live_only else ("active", "paused")
    async with db.session() as s:
        return list(await s.scalars(select(Partnership).where(Partnership.offer_id == offer.id,
                                                              Partnership.status.in_(sts)).order_by(Partnership.id)))


async def my_schedule(db: Database, p: Partnership, poster_id: int) -> Schedule | None:
    pid = p.offer_id if p.offer_id and poster_id == p.a_child_id else p.id  # quem convidou usa a do modelo
    async with db.session() as s:
        return await s.scalar(select(Schedule).where(Schedule.partnership_id == pid, Schedule.child_id == poster_id))


async def schedule_pids(db: Database, sch: Schedule) -> list[int]:
    """Ids de parceria cujos grupos esta divulgação pode usar."""
    p = await get_p(db, sch.partnership_id or 0)
    if p is not None and is_offer(p):
        return [m.id for m in await members(db, p)]
    return [sch.partnership_id or 0]


async def chosen_chats(db: Database, p: Partnership, owner_id: int) -> list[PartnerChat]:
    async with db.session() as s:
        return list(await s.scalars(select(PartnerChat).where(PartnerChat.partnership_id == p.id,
                                                              PartnerChat.owner_child_id == owner_id)))


async def presence(db: Database, p: Partnership, poster_id: int, chat_id: int) -> ChildChat | None:
    """A linha do bot de quem posta dentro do grupo do parceiro (se ele já entrou)."""
    async with db.session() as s:
        return await s.scalar(select(ChildChat).where(ChildChat.child_id == poster_id, ChildChat.chat_id == chat_id,
                                                      ChildChat.partner_of == p.id))


async def _sync(db: Database, sch: Schedule | None, poster: int, pairs: list[tuple[int, int]]) -> None:
    """Destinos = grupos que o outro lado liberou, onde o bot de quem posta já entrou."""
    if sch is None:
        return
    want: set[int] = set()
    async with db.session() as s:
        for pid, owner in pairs:
            allowed = set(await s.scalars(select(PartnerChat.chat_id).where(PartnerChat.partnership_id == pid,
                                                                            PartnerChat.owner_child_id == owner)))
            rows = await s.scalars(select(ChildChat).where(
                ChildChat.child_id == poster, ChildChat.partner_of == pid,
                ChildChat.bot_status.in_(("administrator", "member", "creator"))))
            want |= {r.id for r in rows if r.chat_id in allowed}
        have = {d.chat_row_id: d for d in await s.scalars(select(ScheduleDest).where(ScheduleDest.schedule_id == sch.id))}
        for rid in want - set(have):
            s.add(ScheduleDest(schedule_id=sch.id, chat_row_id=rid))
        for rid, d in have.items():
            if rid not in want:
                await s.delete(d)
        await s.commit()


async def sync_dests(db: Database, p: Partnership) -> None:
    if is_offer(p):
        offer = p
    elif p.offer_id:
        if p.b_child_id:
            await _sync(db, await my_schedule(db, p, p.b_child_id), p.b_child_id, [(p.id, p.a_child_id)])
        offer = await get_p(db, p.offer_id)
    else:  # parceria antiga: um par
        for poster, owner in ((p.a_child_id, p.b_child_id), (p.b_child_id, p.a_child_id)):
            if poster is not None and owner is not None:
                await _sync(db, await my_schedule(db, p, poster), poster, [(p.id, owner)])
        return
    if offer is not None:
        pairs = [(m.id, m.b_child_id) for m in await members(db, offer, live_only=True)]
        await _sync(db, await my_schedule(db, offer, offer.a_child_id), offer.a_child_id, pairs)


async def partner_admitting(db: Database, child: ChildBot, adder_id: int, chat_id: int) -> Partnership | None:
    """O bot foi colocado num grupo por alguém de um parceiro ativo, num grupo liberado na parceria?"""
    async with db.session() as s:
        ps = list(await s.scalars(select(Partnership).where(
            Partnership.status.in_(("active", "paused")),
            or_(Partnership.a_child_id == child.id, Partnership.b_child_id == child.id))))
    for p in ps:
        owner = other_id(p, child.id)
        owner_bot = await bot_row(db, owner)
        if owner_bot is None or not await repo.is_child_admin(db, owner_bot, adder_id):
            continue
        if any(c.chat_id == chat_id for c in await chosen_chats(db, p, owner)):
            return p
    return None


async def enforce(db: Database, sch: Schedule, now_local: datetime, force: bool) -> tuple[Partnership | None, str]:
    """Regras do acordo na hora de postar. Devolve (parceria, motivo para NÃO postar)."""
    p = await get_p(db, sch.partnership_id or 0)
    if p is None:
        return p, "a parceria não existe mais"
    if is_offer(p):
        if p.status == "ended":
            return p, "a parceria foi encerrada"
    elif p.status != "active":
        return p, "a parceria não está ativa"
    if p.end_date and now_local.date().isoformat() > p.end_date:
        return p, "a parceria acabou (data final)"
    minute = now_local.hour * 60 + now_local.minute
    ws, we = p.window_start, p.window_end
    if not (ws == 0 and we >= 1440):
        inside = ws <= minute < we if ws < we else (minute >= ws or minute < we)
        if not inside:
            return p, "fora do horário combinado na parceria"
    if sch.last_run_at is not None:
        from .scheduling import to_local
        elapsed = (now_local - to_local(sch.last_run_at, now_local.tzinfo)).total_seconds() / 60
        if elapsed < p.min_interval - 1:
            return p, f"o acordo é de no mínimo {interval_text(p.min_interval)} entre posts"
    today = now_local.date().isoformat()
    if p.max_day and sch.posts_day == today and sch.posts_today >= p.max_day:
        return p, f"o acordo é de até {p.max_day} post(s) por dia"
    return p, ""


def watermark(mother_username: str, child_id: int) -> str:
    link = f"https://t.me/{mother_username}?start=parc_{child_id}" if mother_username else ""
    label = "🤝 Parcerias automáticas no Telegram"
    return f'<a href="{link}">{label}</a>' if link else label


def _sch_terms(sch: Schedule, p: Partnership) -> None:
    sch.interval_min = p.min_interval if not sch.enabled else max(sch.interval_min or 0, p.min_interval)
    if p.max_day and (not sch.max_per_day or sch.max_per_day > p.max_day):
        sch.max_per_day = p.max_day
    sch.window_start, sch.window_end, sch.end_date = p.window_start, p.window_end, p.end_date


async def apply_terms(db: Database, p: Partnership) -> None:
    """Acordo mudou: a divulgação ligada a ele acompanha."""
    async with db.session() as s:
        for sch in await s.scalars(select(Schedule).where(Schedule.partnership_id == p.id)):
            _sch_terms(sch, p)
        await s.commit()


async def new_offer(db: Database, child: ChildBot) -> Partnership:
    async with db.session() as s:
        n = len(list(await s.scalars(select(Partnership.id).where(Partnership.a_child_id == child.id,
                                                                    Partnership.offer_id.is_(None)))))
        p = Partnership(code=new_code(), a_child_id=child.id, status="draft", name=f"Parceria {n + 1}")
        s.add(p)
        await s.flush()
        sch = Schedule(child_id=child.id, kind="parc", partnership_id=p.id, name=f"Divulgação · {p.name}",
                       start_time="00:00", enabled=False)
        _sch_terms(sch, p)
        s.add(sch)
        await s.commit()
        return p


# ---------- aceitar ----------

async def accept(db: Database, manager, p: Partnership, b: ChildBot, user_id: int) -> str:
    """Aceita o convite com o bot B. Devolve texto de erro, ou '' se deu certo."""
    if p.status != "invited":
        return "Esse convite não está mais valendo."
    if b.id == p.a_child_id:
        return "Esse convite é do seu próprio bot. Aceite com outro bot."
    if not await repo.is_child_admin(db, b, user_id):
        return "Você não é dono nem admin desse bot."
    async with db.session() as s:
        dup = await s.scalar(select(Partnership.id).where(
            Partnership.status.in_(("active", "paused")), Partnership.b_child_id == b.id,
            or_(Partnership.offer_id == p.id,
                Partnership.offer_id.is_(None) & (Partnership.a_child_id == p.a_child_id))))
        if dup:
            return "Esse bot já está nessa parceria."
        row = Partnership(code=new_code(), a_child_id=p.a_child_id, b_child_id=b.id, offer_id=p.id,
                          status="active", accepted_at=utcnow(),
                          **{k: getattr(p, k) for k in TERMS})
        s.add(row)
        await s.flush()
        for c in await s.scalars(select(PartnerChat).where(PartnerChat.partnership_id == p.id)):
            s.add(PartnerChat(partnership_id=row.id, owner_child_id=c.owner_child_id, chat_id=c.chat_id,
                              title=c.title, type=c.type))
        sch = Schedule(child_id=b.id, kind="parc", partnership_id=row.id, name=f"Divulgação · {pname(p)}",
                       start_time="00:00", enabled=False)
        _sch_terms(sch, row)
        s.add(sch)
        if not await s.scalar(select(Schedule.id).where(Schedule.partnership_id == p.id)):
            a_sch = Schedule(child_id=p.a_child_id, kind="parc", partnership_id=p.id, name=f"Divulgação · {pname(p)}",
                             start_time="00:00", enabled=False)
            _sch_terms(a_sch, p)
            s.add(a_sch)
        await s.commit()
    a = await bot_row(db, p.a_child_id)
    await _tell(manager, a, f"🤝 <b>Novo parceiro!</b>\n\n@{escape(b.username)} entrou na sua parceria "
                            f"«{escape(pname(p))}». Abra 🤝 Parcerias no painel do @{escape(a.username)} para ver "
                            "e colocar o bot dele nos seus grupos liberados.")
    logger.info("Parceria %s: @%s entrou (parceiro %s)", p.id, b.username, row.id)
    return ""


async def find_member(db: Database, offer_id: int, b_id: int) -> Partnership | None:
    async with db.session() as s:
        return await s.scalar(select(Partnership).where(Partnership.offer_id == offer_id, Partnership.b_child_id == b_id,
                                                        Partnership.status.in_(("active", "paused"))))


async def _tell(manager, child: ChildBot | None, text: str) -> None:
    if child is None:
        return
    for sender in (manager.bot_for(child.id), manager.mother):
        if sender is None:
            continue
        try:
            await sender.send_message(child.owner_id, text, link_preview_options={"is_disabled": True})
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            continue


async def owner_label(manager, b: ChildBot | None) -> str:
    """@ do dono do bot (se der para ver), para os parceiros conversarem."""
    if b is None:
        return "?"
    for sender in (manager.mother, manager.bot_for(b.id)):
        if sender is None:
            continue
        try:
            chat = await sender.get_chat(b.owner_id)
        except (TelegramBadRequest, TelegramForbiddenError, AttributeError):
            continue
        if getattr(chat, "username", None):
            return f"@{escape(chat.username)}"
        name = " ".join(x for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if x)
        if name:
            return f'<a href="tg://user?id={b.owner_id}">{escape(name)}</a>'
    return f'<a href="tg://user?id={b.owner_id}">{b.owner_id}</a>'


async def _leave(db: Database, manager, rows: list[ChildChat]) -> None:
    for r in rows:
        bot = manager.bot_for(r.child_id)
        if bot is not None:
            try:
                await bot.leave_chat(r.chat_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        async with db.session() as s:
            x = await s.get(ChildChat, r.id)
            x.bot_status = "left"
            await s.commit()


async def end_partnership(db: Database, manager, p: Partnership, by_child: int) -> None:
    async with db.session() as s:
        row = await s.get(Partnership, p.id)
        row.status, row.ended_by = "ended", by_child
        for sch in await s.scalars(select(Schedule).where(Schedule.partnership_id == p.id)):
            sch.enabled, sch.next_run_at = False, None
        rows = list(await s.scalars(select(ChildChat).where(ChildChat.partner_of == p.id)))
        await s.commit()
    await _leave(db, manager, rows)  # o bot de cada um sai dos grupos do outro
    await sync_dests(db, await get_p(db, p.id))
    me, other = await bot_row(db, by_child), await bot_row(db, other_id(p, by_child))
    if other is not None:
        await _tell(manager, other, f"❌ A parceria «{escape(pname(p))}» com @{escape(me.username)} foi encerrada por "
                                    "eles. O bot de cada um saiu dos grupos do outro.")


async def end_offer(db: Database, manager, offer: Partnership, by_child: int) -> None:
    for m in await members(db, offer):
        await end_partnership(db, manager, m, by_child)
    async with db.session() as s:
        row = await s.get(Partnership, offer.id)
        row.status, row.ended_by = "ended", by_child
        for sch in await s.scalars(select(Schedule).where(Schedule.partnership_id == offer.id)):
            sch.enabled, sch.next_run_at = False, None
        await s.commit()


async def on_bot_lost(db: Database, manager, child: ChildBot, p: Partnership, title: str, removed: bool) -> None:
    """O bot de um parceiro foi tirado de admin (ou removido) de um grupo liberado: avisa os dois."""
    owner = await bot_row(db, other_id(p, child.id))
    what = "removido do" if removed else "tirado de administrador no"
    msg = (f"⚠️ <b>Parceria «{escape(pname(p))}»</b>\n\nO @{escape(child.username)} foi {what} grupo "
           f"<b>{escape(title)}</b>, do @{escape(owner.username if owner else '?')}. "
           + ("Os posts lá param." if removed else "Em canal ele não consegue mais postar."))
    await _tell(manager, child, msg + "\nSe não foi combinado, fale com o parceiro em 🤝 Parcerias → 💬 Mandar mensagem.")
    await _tell(manager, owner, msg)


# ---------- telas ----------

async def screen_list(child: ChildBot, db: Database, manager) -> Screen:
    async with db.session() as s:
        ps = list(await s.scalars(select(Partnership).where(
            Partnership.status != "ended",
            or_((Partnership.a_child_id == child.id) & Partnership.offer_id.is_(None),
                Partnership.b_child_id == child.id)).order_by(Partnership.id)))
    lines = ["🤝 <b>Parcerias</b>\n",
             "Troque divulgação com outros grupos e canais. Cada parceria é um acordo (de quanto em quanto tempo, "
             "posts por dia, horário, tamanho do texto, requisitos) com um convite que serve para <b>vários "
             "parceiros</b>. Para outro acordo (ex.: «1000 membros» e «2000 membros»), crie outra parceria.\n",
             "<b>Começar</b>\n"
             "• ➕ Nova parceria: monte o acordo, a sua divulgação e mande o convite.\n"
             "• 📥 Tenho um convite: cole o código que o parceiro te mandou.\n"]
    rows = []
    for p in ps:
        if is_offer(p):
            n = len(await members(db, p))
            lines.append(f"{STATUS[p.status]} · <b>{escape(pname(p))}</b> · {n} parceiro(s)")
            rows.append([cbtn(f"{STATUS[p.status].split()[0]} {pname(p)} ({n})"[:50], "pt", "", p.id)])
        else:
            other = await bot_row(db, other_id(p, child.id))
            name = f"@{other.username}" if other else "?"
            lines.append(f"{STATUS[p.status]} · {escape(pname(p) if p.name else '')} com {escape(name)}")
            rows.append([cbtn(f"{STATUS[p.status].split()[0]} {name}" + (f" · {p.name}" if p.name else ""), "pt", "", p.id)])
    if not ps:
        lines.append("<i>Nenhuma parceria ainda.</i>")
    return "\n".join(lines), kb([cbtn("➕ Nova parceria", "parc", "new", style="primary"),
                                 cbtn("📥 Tenho um convite", "parc", "code")], *rows, [cbtn("« Painel", "home")])


async def _group_line(db: Database, p: Partnership, poster_id: int | None, chat_id: int, title: str,
                      mine: bool) -> str:
    """Situação de um grupo: o bot entrou? é admin? postou quando?"""
    inside = await presence(db, p, poster_id, chat_id) if poster_id else None
    if inside is None or inside.bot_status not in ("administrator", "member", "creator"):
        return f"❌ {escape(title)} — " + ("falta adicionar o bot dele" if mine else "esperando ele adicionar seu bot")
    async with db.session() as s:
        last = await s.scalar(select(ScheduleDest.last_sent_at).join(Schedule, Schedule.id == ScheduleDest.schedule_id)
                              .where(ScheduleDest.chat_row_id == inside.id, Schedule.child_id == poster_id)
                              .order_by(ScheduleDest.last_sent_at.desc()).limit(1))
    admin = inside.bot_status in ("administrator", "creator")
    tail = f"postou {ago(last)}" if last else "ainda não postou"
    icon = "✅" if last and (utcnow() - last).days < 2 else "⚠️"
    return f"{icon} {escape(title)} — {tail}" + ("" if admin else " · ⚠️ não é admin")


async def screen_offer(child: ChildBot, db: Database, manager, settings, p: Partnership) -> Screen:
    mine = await chosen_chats(db, p, child.id)
    ms = await members(db, p)
    sch = await my_schedule(db, p, child.id)
    i = p.id
    lines = [f"🤝 <b>{escape(pname(p))}</b> · {STATUS[p.status]}\n",
             "Monte aqui o acordo, os requisitos e a sua divulgação. O mesmo convite serve para vários parceiros: "
             "cada um que aceitar aparece na lista abaixo.\n",
             "<b>Acordo</b>", terms_text(p), "", "<b>Requisitos</b> (o parceiro vê no convite)", req_text(p), "",
             f"<b>Seus grupos liberados para os parceiros</b>: {len(mine)}"]
    lines += [f"• {escape(c.title)}" for c in mine]
    if sch is not None:
        lines.append(f"\n<b>Sua divulgação</b> (vai para os grupos de todos os parceiros): "
                     f"{'Ativada ✅' if sch.enabled else 'Desativada ❌'} · próximo: {next_text(sch, settings.tz)}"
                     + (f"\n⚠️ {escape(sch.last_error)}" if sch.last_error else ""))
    lines.append(f"\n<b>Parceiros</b> ({len(ms)})")
    for m in ms:
        o = await bot_row(db, m.b_child_id)
        lines.append(f"{STATUS[m.status].split()[0]} @{escape(o.username if o else '?')}")
    if not ms:
        lines.append("<i>ninguém ainda</i>")
    if p.status in ("invited", "closed"):
        link = f"https://t.me/{manager.mother_username}?start=pc_{p.code}"
        lines.append(f"\n📨 <b>Convite</b> {'(aberto)' if p.status == 'invited' else '(fechado: ninguém novo entra)'}\n"
                     f"{link}\nOu o código <code>{p.code}</code> (painel do bot → 🤝 Parcerias → 📥 Tenho um convite).")
    if ms:
        lines.append("\n🔒 Já tem parceiros: o acordo não muda mais. Para outro acordo, crie outra parceria.")
    rows = [[cbtn("✏️ Nome", "ptt", "name", i), cbtn("📋 Requisitos", "ptt", "req", i)],
            [cbtn("⏱ Intervalo mínimo", "ptt", "int", i), cbtn("🔢 Máx. por dia", "ptt", "max", i)],
            [cbtn("⏰ Horário", "ptt", "win", i), cbtn("📆 Até quando", "ptt", "end", i)],
            [cbtn("✂️ Máx. caracteres", "ptt", "chars", i), cbtn("📏 Máx. linhas", "ptt", "lines", i)],
            [cbtn(f"{'✅' if p.allow_media else '❌'} Foto/vídeo", "ptt", "media", i),
             cbtn(f"{'✅' if p.allow_buttons else '❌'} Botões", "ptt", "btns", i)],
            [cbtn(f"👥 Meus grupos liberados ({len(mine)})", "ptg", "", i)]]
    if sch is not None:
        rows.append([cbtn("📣 Minha divulgação", "sch", "", sch.id)])
    if p.status == "draft":
        rows.append([cbtn("📨 Gerar convite", "ptt", "invite", i, style="primary")])
    elif p.status == "invited":
        rows.append([cbtn("🔒 Fechar convite", "ptt", "close", i)])
    elif p.status == "closed":
        rows.append([cbtn("🔓 Abrir convite", "ptt", "invite", i)])
    rows += [[cbtn(f"{STATUS[m.status].split()[0]} Parceiro {n}", "pt", "", m.id) for n, m in list(enumerate(ms, 1))[x:x + 3]]
             for x in range(0, len(ms), 3)]
    rows += [[cbtn("🗑 Apagar parceria", "ptt", "del1", i)], [cbtn("« Parcerias", "parc")]]
    return "\n".join(lines)[:4000], kb(*rows)


async def screen_active(child: ChildBot, db: Database, manager, settings, p: Partnership) -> Screen:
    other = await bot_row(db, other_id(p, child.id))
    a_side = p.a_child_id == child.id
    groups_pid = p.offer_id if (p.offer_id and a_side) else p.id  # quem convidou libera grupos no modelo
    mine = await chosen_chats(db, p, child.id)
    theirs = await chosen_chats(db, p, other.id) if other else []
    sch = await my_schedule(db, p, child.id)
    oname = escape(other.username if other else "?")
    lines = [f"🤝 <b>{escape(pname(p))}</b> · parceiro @{oname}\n",
             f"👤 Dono do @{oname}: {await owner_label(manager, other)}",
             f"Situação: {STATUS[p.status]}" + (" (por você)" if p.status == "paused" and p.paused_by == child.id else
                                                 " (pelo parceiro)" if p.status == "paused" else ""),
             "", "<b>Acordo</b>", terms_text(p), "",
             f"<b>Seus grupos onde o @{oname} posta</b>"]
    for c in mine:
        lines.append(await _group_line(db, p, other.id if other else None, c.chat_id, c.title, True))
    if not mine:
        lines.append("<i>nenhum: escolha em 👥 Meus grupos liberados</i>")
    lines.append(f"\n<b>Grupos do @{oname} onde você posta</b>")
    for c in theirs:
        lines.append(await _group_line(db, p, child.id, c.chat_id, c.title, False))
    if not theirs:
        lines.append("<i>o parceiro ainda não liberou grupos</i>")
    if sch is not None:
        lines.append(f"\n<b>Sua divulgação</b>{' (a mesma para todos os parceiros desta parceria)' if p.offer_id and a_side else ''}: "
                     f"{'Ativada ✅' if sch.enabled else 'Desativada ❌'} · próximo: {next_text(sch, settings.tz)} · hoje: "
                     f"{sch.posts_today if sch.posts_day == datetime.now(settings.tz).date().isoformat() else 0}"
                     f"/{p.max_day or '∞'}" + (f"\n⚠️ {escape(sch.last_error)}" if sch.last_error else ""))
    lines.append("\n✅ postou nos últimos 2 dias · ⚠️ está lá mas não postou (ou faz tempo) · ❌ ainda não entrou.\n"
                 "• 💬 Mandar mensagem: o bot entrega a sua mensagem ao dono do outro bot.\n"
                 "• Para mudar o acordo: encerre e combine outra parceria.")
    i = p.id
    rows = [[cbtn("📣 Minha divulgação", "sch", "", sch.id)]] if sch else []
    rows.append([cbtn("💬 Mandar mensagem", "ptt", "msg", i),
                 cbtn(f"👥 Meus grupos liberados ({len(mine)})", "ptg", "", groups_pid)])
    if other:
        rows.append([url(f"➕ Adicionar @{other.username} num grupo", f"https://t.me/{other.username}?{GROUP_ADD}"),
                     url("➕ Num canal", f"https://t.me/{other.username}?{CHANNEL_ADD}")])
    if p.status == "active":
        rows.append([cbtn("⏸ Pausar", "ptt", "pause", i), cbtn("❌ Encerrar", "ptt", "end1", i)])
    else:
        rows.append([cbtn("▶️ Retomar", "ptt", "resume", i), cbtn("❌ Encerrar", "ptt", "end1", i)])
    rows.append([cbtn(f"« {pname(p)}", "pt", "", p.offer_id) if p.offer_id and a_side else cbtn("« Parcerias", "parc")])
    return "\n".join(lines)[:4000], kb(*rows)


async def screen_groups(child: ChildBot, db: Database, p: Partnership) -> Screen:
    mine = {c.chat_id for c in await chosen_chats(db, p, child.id)}
    chats = [c for c in await repo.child_chats(db, child.id) if c.bot_status in ("administrator", "creator")]
    text = ("👥 <b>Seus grupos liberados para o parceiro</b>\n\n"
            "Toque para liberar ou tirar. Depois coloque o bot do parceiro nesses grupos "
            "(canal: como administrador com permissão de postar).\n"
            "Se tirar um grupo, o bot do parceiro sai dele.")
    if is_offer(p):
        text += "\n\nVale para todos os parceiros desta parceria."
    rows = [[cbtn(("✅ " if c.chat_id in mine else "▫️ ") + ("📢 " if c.type == ChatType.CHANNEL else "👥 ")
                  + (c.title or str(c.chat_id))[:40], "ptg", "tog", p.id, j=c.id)] for c in chats]
    if not chats:
        text += "\n\n<i>O bot ainda não é administrador em nenhum grupo seu.</i>"
    return text, kb(*rows, [cbtn("« Voltar", "pt", "", p.id)])


async def screen_detail(child: ChildBot, db: Database, manager, settings, p: Partnership) -> Screen:
    if is_offer(p) and p.a_child_id == child.id:
        return await screen_offer(child, db, manager, settings, p)
    return await screen_active(child, db, manager, settings, p)


def preview_invite(p: Partnership, a: ChildBot) -> str:
    return (f"🤝 <b>Convite de parceria</b> · {escape(pname(p))}\n\n@{escape(a.username)} quer trocar divulgação "
            f"com você.\n\n<b>Acordo</b>\n{terms_text(p)}\n\n<b>Requisitos</b>\n{req_text(p)}\n\n"
            "Aceitando, você escolhe em quais grupos seus ele posta e monta a sua divulgação nos grupos dele.")


# ---------- botões ----------

def _choice_screen(title: str, help_: str, opts: list[tuple[str, int]], cur: int, a: str, i: int,
                   custom: str) -> Screen:
    btns = [cbtn(("✅ " if cur == v else "") + lbl, "ptt", a, i, j=v) for lbl, v in opts]
    return (f"{title}\n\n{help_}", kb(*[btns[x:x + 4] for x in range(0, len(btns), 4)],
                                       [cbtn("✏️ Outro número", "ptt", custom, i)], [cbtn("« Voltar", "pt", "", i)]))


@router.callback_query(CP.filter(F.s.in_({"parc", "pt", "ptt", "ptg"})))
async def on_partners(cb: CallbackQuery, callback_data: CP, state: FSMContext, bot: Bot, child: ChildBot,
                      db: Database, settings, manager) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    s_, a, i, j = callback_data.s, callback_data.a, callback_data.i, callback_data.j
    await state.clear()
    if s_ == "parc":
        if a == "new":
            p = await new_offer(db, child)
            await cb.answer()
            return await show(cb, await screen_offer(child, db, manager, settings, p))
        if a == "code":
            return await ask(cb, state, "parc_code", "📥 <b>Tenho um convite</b>\n\nCole o código que o parceiro te "
                             "mandou (ex.: <code>A1B2C3D4</code>).", cbtn("❌ Cancelar", "parc"))
        await cb.answer()
        return await show(cb, await screen_list(child, db, manager))

    p = await get_p(db, i)
    if p is None or child.id not in (p.a_child_id, p.b_child_id) or p.status == "ended":
        await cb.answer("Parceria não encontrada.", show_alert=True)
        return await show(cb, await screen_list(child, db, manager))
    offer = is_offer(p) and p.a_child_id == child.id
    locked = offer and bool(await members(db, p))

    async def save(**fields):
        async with db.session() as s:
            row = await s.get(Partnership, p.id)
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()
        if any(k in fields for k in ("min_interval", "max_day", "window_start", "window_end", "end_date")):
            await apply_terms(db, await get_p(db, p.id))

    note = None
    if s_ == "ptg":
        if a == "tog":
            async with db.session() as s:
                c = await s.get(ChildChat, j)
                if c is None or c.child_id != child.id:
                    await cb.answer("Não encontrado.", show_alert=True)
                    return
            targets = [p] + (await members(db, p) if offer else [])  # no modelo: vale para todos os parceiros
            async with db.session() as s:
                row = await s.scalar(select(PartnerChat).where(PartnerChat.partnership_id == p.id,
                                                               PartnerChat.chat_id == c.chat_id))
                removed = row is not None
            for t in targets:
                async with db.session() as s:
                    r = await s.scalar(select(PartnerChat).where(PartnerChat.partnership_id == t.id,
                                                                 PartnerChat.chat_id == c.chat_id))
                    if removed and r is not None:
                        await s.delete(r)
                    elif not removed and r is None:
                        s.add(PartnerChat(partnership_id=t.id, owner_child_id=child.id, chat_id=c.chat_id,
                                          title=c.title or "", type=c.type))
                    await s.commit()
                o = other_id(t, child.id)
                if removed and o:
                    inside = await presence(db, t, o, c.chat_id)
                    if inside is not None:
                        await _leave(db, manager, [inside])
                await sync_dests(db, t)
        await cb.answer()
        return await show(cb, await screen_groups(child, db, await get_p(db, p.id)))

    if s_ == "ptt":
        terms = {"int", "setint", "max", "setmax", "maxc", "win", "allday", "wintime", "end", "nodate", "enddate",
                 "chars", "setchars", "charsc", "lines", "setlines", "linesc", "media", "btns"}
        config = terms | {"name", "req", "reqopen", "reqmem", "reqnote", "invite", "close", "del1", "del2"}
        if a in config and not offer:
            await cb.answer("O acordo só muda na parceria de quem convidou. Para mudar, encerre e combine outra.",
                            show_alert=True)
            return
        if a in terms and locked:
            await cb.answer("Essa parceria já tem parceiros: o acordo não muda mais. Para outro acordo, crie outra "
                            "parceria (ex.: «2000 membros»).", show_alert=True)
            return
        back = cbtn("❌ Cancelar", "pt", "", i)
        if a == "name":
            return await ask(cb, state, "parc_name", "✏️ <b>Nome da parceria</b>\n\nEx.: <code>1000 membros</code>.",
                             back, pid=i)
        if a == "req":
            await cb.answer()
            return await show(cb, ("📋 <b>Requisitos</b>\n\nO que você pede de quem for entrar. Aparece no convite, "
                                   "para a pessoa ver antes de aceitar.\n\n" + req_text(p),
                                   kb([cbtn(f"{'✅' if p.req_open else '❌'} 💬 Chat aberto obrigatório", "ptt", "reqopen", i)],
                                      [cbtn(f"👥 Mínimo de membros: {p.req_members or 'não pede'}", "ptt", "reqmem", i)],
                                      [cbtn("📝 Outros requisitos (texto)", "ptt", "reqnote", i)],
                                      [cbtn("« Voltar", "pt", "", i)])))
        if a == "reqopen":
            await save(req_open=not p.req_open)
            await cb.answer()
            p = await get_p(db, i)
            return await show(cb, ("📋 <b>Requisitos</b>\n\n" + req_text(p),
                                   kb([cbtn(f"{'✅' if p.req_open else '❌'} 💬 Chat aberto obrigatório", "ptt", "reqopen", i)],
                                      [cbtn(f"👥 Mínimo de membros: {p.req_members or 'não pede'}", "ptt", "reqmem", i)],
                                      [cbtn("📝 Outros requisitos (texto)", "ptt", "reqnote", i)],
                                      [cbtn("« Voltar", "pt", "", i)])))
        if a == "reqmem":
            return await ask(cb, state, "parc_reqmem", "👥 <b>Mínimo de membros</b>\n\nEnvie o número (ex.: "
                             "<code>1000</code>). Envie <code>0</code> para não pedir.", back, pid=i)
        if a == "reqnote":
            return await ask(cb, state, "parc_reqnote", "📝 <b>Outros requisitos</b>\n\nEscreva o que mais você pede "
                             "(ex.: <i>grupo de games, sem conteúdo adulto</i>). Envie <code>-</code> para apagar.",
                             back, pid=i)
        if a == "int":
            await cb.answer()
            btns = [cbtn(("✅ " if p.min_interval == m else "") + interval_text(m), "ptt", "setint", i, j=m)
                    for m in INTERVALS]
            return await show(cb, ("⏱ <b>Intervalo mínimo entre posts</b>\n\nO parceiro não consegue postar mais "
                                   "rápido que isso em cada grupo.", kb(btns[:4], btns[4:], [cbtn("« Voltar", "pt", "", i)])))
        if a == "setint" and j in INTERVALS:
            await save(min_interval=j)
        elif a == "max":
            await cb.answer()
            return await show(cb, _choice_screen("🔢 <b>Máximo de posts por dia</b> (em cada grupo)",
                                                 "♾ Sem limite: vale só o intervalo mínimo.",
                                                 [(str(m), m) for m in MAXES] + [("♾ Sem limite", 0)],
                                                 p.max_day, "setmax", i, "maxc"))
        elif a == "setmax" and (j == 0 or j in MAXES):
            await save(max_day=j)
        elif a == "maxc":
            return await ask(cb, state, "parc_max", "🔢 <b>Máximo por dia</b>\n\nEnvie o número de posts por dia em "
                             "cada grupo (ex.: <code>100</code>). <code>0</code> = sem limite.", back, pid=i)
        elif a in ("win", "allday"):
            if a == "allday":
                await save(window_start=0, window_end=1440)
                p = await get_p(db, i)
            await cb.answer()
            full = p.window_start == 0 and p.window_end >= 1440
            return await show(cb, ("⏰ <b>Horário combinado</b>\n\nEm que horas os posts da parceria podem sair.\n\n"
                                   "Agora: " + ("dia todo" if full else f"{hhmm(p.window_start)} às {hhmm(p.window_end)}"),
                                   kb([cbtn(("✅ " if full else "▫️ ") + "Dia todo", "ptt", "allday", i,
                                            style="success" if full else None)],
                                      [cbtn(("▫️ " if full else "✅ ") + "⏰ Definir horário", "ptt", "wintime", i,
                                            style=None if full else "success")],
                                      [cbtn("« Voltar", "pt", "", i)])))
        elif a == "wintime":
            return await ask(cb, state, "parc_win", "⏰ <b>Definir horário</b>\n\nEnvie o começo e o fim, assim: "
                             "<code>08:00-23:00</code>.", cbtn("❌ Cancelar", "ptt", "win", i), pid=i)
        elif a in ("end", "nodate"):
            if a == "nodate":
                await save(end_date=None)
                p = await get_p(db, i)
            await cb.answer()
            nod = not p.end_date
            return await show(cb, ("📆 <b>Até quando vale</b>\n\nAgora: " +
                                   ("sem data" if nod else "até " + "/".join(reversed(p.end_date.split("-")))),
                                   kb([cbtn(("✅ " if nod else "▫️ ") + "Sem data", "ptt", "nodate", i,
                                            style="success" if nod else None)],
                                      [cbtn(("▫️ " if nod else "✅ ") + "📆 Definir data", "ptt", "enddate", i,
                                            style=None if nod else "success")],
                                      [cbtn("« Voltar", "pt", "", i)])))
        elif a == "enddate":
            return await ask(cb, state, "parc_end", "📆 <b>Definir data</b>\n\nEnvie a data final, assim: "
                             "<code>31/12/2026</code>.", cbtn("❌ Cancelar", "ptt", "end", i), pid=i)
        elif a == "chars":
            await cb.answer()
            return await show(cb, _choice_screen("✂️ <b>Máximo de caracteres</b> no texto",
                                                 "Vale para os dois lados. Post maior que isso não sai.",
                                                 [(str(m), m) for m in CHARS] + [("♾ Sem limite", 0)],
                                                 p.max_chars or 0, "setchars", i, "charsc"))
        elif a == "setchars" and (j == 0 or j in CHARS):
            await save(max_chars=j)
        elif a == "charsc":
            return await ask(cb, state, "parc_chars", "✂️ <b>Máximo de caracteres</b>\n\nEnvie o número (ex.: "
                             "<code>600</code>). <code>0</code> = sem limite.", back, pid=i)
        elif a == "lines":
            await cb.answer()
            return await show(cb, _choice_screen("📏 <b>Máximo de linhas</b> no texto",
                                                 "Define a altura da mensagem na tela. Vale para os dois lados: "
                                                 "post com mais linhas não sai.",
                                                 [(str(m), m) for m in LINES] + [("♾ Sem limite", 0)],
                                                 p.max_lines or 0, "setlines", i, "linesc"))
        elif a == "setlines" and (j == 0 or j in LINES):
            await save(max_lines=j)
        elif a == "linesc":
            return await ask(cb, state, "parc_lines", "📏 <b>Máximo de linhas</b>\n\nEnvie o número (ex.: "
                             "<code>12</code>). <code>0</code> = sem limite.", back, pid=i)
        elif a == "media":
            await save(allow_media=not p.allow_media)
        elif a == "btns":
            await save(allow_buttons=not p.allow_buttons)
        elif a == "invite":
            if not await chosen_chats(db, p, child.id):
                await cb.answer("Escolha antes pelo menos um grupo seu para o parceiro postar (👥 Meus grupos liberados).",
                                show_alert=True)
                return
            await save(status="invited")
            note = "Convite aberto. Mande o link para quem quiser: serve para vários parceiros."
        elif a == "close" and p.status == "invited":
            await save(status="closed")
            note = "Convite fechado: ninguém novo entra. Quem já está continua."
        elif a == "del1":
            await cb.answer()
            return await show(cb, ("🗑 <b>Apagar a parceria?</b>\n\nTodos os parceiros dela são encerrados: os posts "
                                   "param e o bot de cada um sai dos grupos do outro.",
                                   kb([cbtn("🗑 Sim, apagar", "ptt", "del2", i, style="danger"),
                                       cbtn("« Voltar", "pt", "", i)])))
        elif a == "del2":
            await end_offer(db, manager, p, child.id)
            await cb.answer("Parceria apagada.")
            return await show(cb, await screen_list(child, db, manager))
        elif a == "msg" and not offer:
            return await ask(cb, state, "parc_msg", "💬 <b>Mensagem para o parceiro</b>\n\nEscreva. O bot entrega "
                             "ao dono do outro bot, com o seu bot como remetente.", back, pid=i)
        elif a == "pause" and p.status == "active":
            await save(status="paused", paused_by=child.id)
            await sync_dests(db, await get_p(db, i))
            await _tell(manager, await bot_row(db, other_id(p, child.id)),
                        f"⏸ @{escape(child.username)} pausou a parceria «{escape(pname(p))}». "
                        "Nenhum dos dois posta até retomar.")
            note = "Pausada: nenhum dos dois posta até retomar."
        elif a == "resume" and p.status == "paused":
            if p.paused_by not in (None, child.id):
                await cb.answer("Quem pausou foi o parceiro: só ele retoma.", show_alert=True)
                return
            await save(status="active", paused_by=None)
            await sync_dests(db, await get_p(db, i))
            await _tell(manager, await bot_row(db, other_id(p, child.id)),
                        f"▶️ @{escape(child.username)} retomou a parceria «{escape(pname(p))}».")
            note = "Retomada."
        elif a == "end1":
            await cb.answer()
            return await show(cb, ("❌ <b>Encerrar com este parceiro?</b>\n\nOs posts param, e o bot de cada um sai "
                                   "dos grupos do outro. Para voltar, é preciso aceitar um convite de novo.",
                                   kb([cbtn("❌ Sim, encerrar", "ptt", "end2", i, style="danger"),
                                       cbtn("« Voltar", "pt", "", i)])))
        elif a == "end2":
            await end_partnership(db, manager, p, child.id)
            await cb.answer("Parceria encerrada.")
            if p.offer_id and p.a_child_id == child.id:
                return await show(cb, await screen_offer(child, db, manager, settings, await get_p(db, p.offer_id)))
            return await show(cb, await screen_list(child, db, manager))
    await cb.answer(note)
    await show(cb, await screen_detail(child, db, manager, settings, await get_p(db, p.id)))


async def handle_input(kind: str, data: dict, message: Message, child: ChildBot, db: Database, settings,
                       manager) -> Screen | str:
    raw = (message.text or "").strip()
    if kind == "parc_code":
        async with db.session() as s:
            p = await s.scalar(select(Partnership).where(Partnership.code == raw.upper()))
        if p is None:
            return "Código não encontrado. Confira e mande de novo, ou /cancelar."
        err = await accept(db, manager, p, child, message.from_user.id)
        if err:
            return err + " Mande outro código ou /cancelar."
        m = await find_member(db, p.id, child.id)
        return await screen_active(child, db, manager, settings, m or await get_p(db, p.id))
    p = await get_p(db, data.get("pid", 0))
    if p is None or child.id not in (p.a_child_id, p.b_child_id) or p.status == "ended":
        return "Parceria não encontrada."
    if kind == "parc_msg":
        if not raw:
            return "Escreva a mensagem ou /cancelar."
        other = await bot_row(db, other_id(p, child.id))
        await _tell(manager, other, f"💬 <b>Mensagem do parceiro @{escape(child.username)}</b> "
                                    f"(parceria «{escape(pname(p))}»)\n\n{escape(raw[:3000])}\n\n"
                                    "Para responder: 🤝 Parcerias → a parceria → 💬 Mandar mensagem.")
        await message.answer("✅ Mensagem entregue ao parceiro.")
        return await screen_active(child, db, manager, settings, p)
    if not is_offer(p) or p.a_child_id != child.id:
        return "Parceria não encontrada."
    num = int(raw) if raw.isdigit() else None
    fields: dict = {}
    if kind == "parc_name":
        if not raw:
            return "Envie o nome ou /cancelar."
        fields["name"] = raw[:60]
    elif kind == "parc_reqmem":
        if num is None:
            return "Envie só o número (ex.: <code>1000</code>) ou /cancelar."
        fields["req_members"] = num
    elif kind == "parc_reqnote":
        fields["req_note"] = "" if raw == "-" else raw[:300]
    elif kind in ("parc_max", "parc_chars", "parc_lines"):
        if num is None or num > 100000:
            return "Envie só o número (ex.: <code>100</code>) ou /cancelar."
        fields[{"parc_max": "max_day", "parc_chars": "max_chars", "parc_lines": "max_lines"}[kind]] = num
    elif kind == "parc_win":
        parts = re.split(r"\s*(?:-|até|às|a)\s*", raw)
        a, b = (parse_hhmm(parts[0]), parse_hhmm(parts[1])) if len(parts) == 2 else (None, None)
        if a is None or b is None or a == b:
            return "Não entendi. Envie assim: <code>08:00-23:00</code> ou /cancelar."
        fields.update(window_start=a, window_end=b or 1440)
    elif kind == "parc_end":
        d = parse_date(raw, datetime.now(settings.tz).date())
        if d is None:
            return "Não entendi. Envie assim: <code>31/12/2026</code> ou /cancelar."
        fields["end_date"] = d.isoformat()
    terms_kinds = ("parc_max", "parc_chars", "parc_lines", "parc_win", "parc_end")
    if kind in terms_kinds and await members(db, p):
        return "Essa parceria já tem parceiros: o acordo não muda mais."
    async with db.session() as s:
        row = await s.get(Partnership, p.id)
        for k, v in fields.items():
            setattr(row, k, v)
        await s.commit()
    if kind in terms_kinds:
        await apply_terms(db, await get_p(db, p.id))
    return await screen_offer(child, db, manager, settings, await get_p(db, p.id))


async def on_partner_bot_joined(db: Database, child: ChildBot, p: Partnership, chat_row_id: int) -> None:
    async with db.session() as s:
        row = await s.get(ChildChat, chat_row_id)
        row.partner_of = p.id
        await s.commit()
    await sync_dests(db, p)


async def reschedule_partner(db: Database, sch_id: int, tz) -> None:
    await reschedule(db, sch_id, tz)
