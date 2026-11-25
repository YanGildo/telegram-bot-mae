"""Parcerias: dois bots filhos combinam um acordo e cada um divulga nos grupos do outro.

Como funciona
1. A cria a proposta (de quanto em quanto tempo, máximo por dia, horário, se pode mídia/botões) e escolhe
   os grupos dele onde B vai poder postar. Gera o convite (código ou link do bot mãe).
2. B aceita com um dos bots dele e escolhe os grupos dele onde A vai postar.
3. Cada um coloca o bot do outro nos grupos que liberou (o painel dá o link).
4. Cada lado monta a sua divulgação (texto, mídias, vários conteúdos, botões) e ativa. O sistema não deixa
   passar do acordo, e todo post de parceria leva a marca d'água com link para o bot mãe.
"""

import logging
import secrets
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import or_, select

from .. import repo
from ..db import ChildBot, ChildChat, Database, PartnerChat, Partnership, Schedule, ScheduleDest, utcnow
from ..ui import CP, Screen, cbtn, kb, show, url
from .flow import ask
from .scheduling import hhmm, interval_text, next_text, parse_date, parse_hhmm, reschedule

logger = logging.getLogger(__name__)
router = Router(name="parcerias")

INTERVALS = [15, 30, 60, 120, 180, 360, 720, 1440]
MAXES = [1, 2, 3, 5, 10, 24]
STATUS = {"draft": "📝 rascunho", "invited": "📨 convite enviado", "active": "🟢 ativa", "paused": "⏸ pausada",
          "ended": "❌ encerrada"}
GROUP_ADD = "startgroup=parceria"
CHANNEL_ADD = "startchannel=true&admin=post_messages+edit_messages+delete_messages"


def new_code() -> str:
    return secrets.token_hex(4).upper()


def other_id(p: Partnership, me: int) -> int | None:
    return p.b_child_id if p.a_child_id == me else p.a_child_id


def terms_text(p: Partnership) -> str:
    window = "dia todo" if p.window_start == 0 and p.window_end >= 1440 else \
        f"{hhmm(p.window_start)} às {hhmm(p.window_end)}"
    content = "texto" + (", foto/vídeo" if p.allow_media else "") + (", botões" if p.allow_buttons else "")
    end = "sem data" if not p.end_date else "até " + "/".join(reversed(p.end_date.split("-")))
    return (f"⏱ No mínimo {interval_text(p.min_interval)} entre posts\n"
            f"🔢 Até {p.max_day} post(s) por dia em cada grupo\n"
            f"⏰ Horário: {window}\n"
            f"📝 Pode ir: {content}\n"
            f"📆 Duração: {end}\n"
            "🤝 Todo post leva a marca d'água das parcerias automáticas")


async def get_p(db: Database, pid: int) -> Partnership | None:
    async with db.session() as s:
        return await s.get(Partnership, pid)


async def bot_row(db: Database, child_id: int | None) -> ChildBot | None:
    if child_id is None:
        return None
    async with db.session() as s:
        return await s.get(ChildBot, child_id)


async def my_schedule(db: Database, p: Partnership, poster_id: int) -> Schedule | None:
    async with db.session() as s:
        return await s.scalar(select(Schedule).where(Schedule.partnership_id == p.id, Schedule.child_id == poster_id))


async def chosen_chats(db: Database, p: Partnership, owner_id: int) -> list[PartnerChat]:
    async with db.session() as s:
        return list(await s.scalars(select(PartnerChat).where(PartnerChat.partnership_id == p.id,
                                                              PartnerChat.owner_child_id == owner_id)))


async def presence(db: Database, p: Partnership, poster_id: int, chat_id: int) -> ChildChat | None:
    """A linha do bot de quem posta dentro do grupo do parceiro (se ele já entrou)."""
    async with db.session() as s:
        return await s.scalar(select(ChildChat).where(ChildChat.child_id == poster_id, ChildChat.chat_id == chat_id,
                                                      ChildChat.partner_of == p.id))


async def sync_dests(db: Database, p: Partnership) -> None:
    """Os destinos da divulgação de cada lado = grupos que o OUTRO liberou e onde o bot já entrou."""
    for poster, owner in ((p.a_child_id, p.b_child_id), (p.b_child_id, p.a_child_id)):
        if poster is None or owner is None:
            continue
        sch = await my_schedule(db, p, poster)
        if sch is None:
            continue
        allowed = {c.chat_id for c in await chosen_chats(db, p, owner)}
        async with db.session() as s:
            rows = list(await s.scalars(select(ChildChat).where(
                ChildChat.child_id == poster, ChildChat.partner_of == p.id,
                ChildChat.bot_status.in_(("administrator", "member", "creator")))))
            want = {r.id for r in rows if r.chat_id in allowed}
            have = {d.chat_row_id: d for d in await s.scalars(select(ScheduleDest).where(ScheduleDest.schedule_id == sch.id))}
            for rid in want - set(have):
                s.add(ScheduleDest(schedule_id=sch.id, chat_row_id=rid))
            for rid, d in have.items():
                if rid not in want:
                    await s.delete(d)
            await s.commit()


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
    if p is None or p.status != "active":
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
    if sch.posts_day == today and sch.posts_today >= p.max_day:
        return p, f"o acordo é de até {p.max_day} post(s) por dia"
    return p, ""


def watermark(mother_username: str, child_id: int) -> str:
    link = f"https://t.me/{mother_username}?start=parc_{child_id}" if mother_username else ""
    label = "🤝 Parcerias automáticas no Telegram"
    return f'<a href="{link}">{label}</a>' if link else label


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
            Partnership.status.in_(("active", "paused")),
            or_((Partnership.a_child_id == p.a_child_id) & (Partnership.b_child_id == b.id),
                (Partnership.a_child_id == b.id) & (Partnership.b_child_id == p.a_child_id))))
        if dup:
            return "Esses dois bots já têm uma parceria ativa."
        row = await s.get(Partnership, p.id)
        row.b_child_id, row.status, row.accepted_at = b.id, "active", utcnow()
        for poster in (row.a_child_id, b.id):
            s.add(Schedule(child_id=poster, kind="parc", partnership_id=row.id, name="Divulgação na parceria",
                           interval_min=row.min_interval, max_per_day=row.max_day, start_time="00:00",
                           window_start=row.window_start, window_end=row.window_end,
                           end_date=row.end_date, enabled=False))
        await s.commit()
    a = await bot_row(db, p.a_child_id)
    await _tell(manager, a, f"🤝 <b>Parceria aceita!</b>\n\n@{escape(b.username)} aceitou o seu convite. "
                            f"Abra 🤝 Parcerias no painel do @{escape(a.username)} para escolher os grupos e montar "
                            "a sua divulgação.")
    logger.info("Parceria %s aceita: @%s × @%s", p.id, a.username if a else "?", b.username)
    return ""


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


async def end_partnership(db: Database, manager, p: Partnership, by_child: int) -> None:
    async with db.session() as s:
        row = await s.get(Partnership, p.id)
        row.status, row.ended_by = "ended", by_child
        for sch in await s.scalars(select(Schedule).where(Schedule.partnership_id == p.id)):
            sch.enabled, sch.next_run_at = False, None
        rows = list(await s.scalars(select(ChildChat).where(ChildChat.partner_of == p.id)))
        await s.commit()
    for r in rows:  # o bot de cada um sai dos grupos do outro
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
    me, other = await bot_row(db, by_child), await bot_row(db, other_id(p, by_child))
    if other is not None:
        await _tell(manager, other, f"❌ A parceria com @{escape(me.username)} foi encerrada por eles. "
                                    "O bot de cada um saiu dos grupos do outro.")


# ---------- telas ----------

async def screen_list(child: ChildBot, db: Database, manager) -> Screen:
    async with db.session() as s:
        ps = list(await s.scalars(select(Partnership).where(
            or_(Partnership.a_child_id == child.id, Partnership.b_child_id == child.id),
            Partnership.status != "ended").order_by(Partnership.id)))
    lines = ["🤝 <b>Parcerias</b>\n",
             "Troque divulgação com outros grupos e canais. Vocês combinam um acordo (de quanto em quanto tempo, "
             "quantos posts por dia, horário e o que pode ir), cada um coloca o bot do outro nos grupos liberados "
             "e o sistema posta sozinho, sem passar do combinado.\n",
             "<b>Começar</b>\n"
             "• ➕ Nova parceria: monte a proposta e mande o convite.\n"
             "• 📥 Tenho um convite: cole o código que o parceiro te mandou.\n"]
    rows = []
    for p in ps:
        other = await bot_row(db, other_id(p, child.id))
        name = f"@{other.username}" if other else "(esperando aceitar)"
        lines.append(f"{STATUS[p.status]} · {escape(name)}")
        rows.append([cbtn(f"{STATUS[p.status].split()[0]} {name}"[:50], "pt", "", p.id)])
    if not ps:
        lines.append("<i>Nenhuma parceria ainda.</i>")
    return "\n".join(lines), kb([cbtn("➕ Nova parceria", "parc", "new", style="primary"),
                                 cbtn("📥 Tenho um convite", "parc", "code")], *rows, [cbtn("« Painel", "home")])


async def screen_draft(child: ChildBot, db: Database, manager, p: Partnership) -> Screen:
    mine = await chosen_chats(db, p, child.id)
    i = p.id
    text = ("📝 <b>Proposta de parceria</b>\n\n"
            "Ajuste o acordo e escolha em quais dos seus grupos o parceiro vai poder postar. "
            "Depois toque em 📨 Gerar convite e mande o link para ele.\n\n"
            f"{terms_text(p)}\n\n"
            f"👥 Seus grupos liberados para o parceiro: {len(mine)}"
            + ("".join(f"\n• {escape(c.title)}" for c in mine)))
    if p.status == "invited":
        link = f"https://t.me/{manager.mother_username}?start=pc_{p.code}"
        text += (f"\n\n📨 <b>Convite</b>\nMande este link para o parceiro:\n{link}\n\n"
                 f"Ou o código <code>{p.code}</code> (no painel do bot dele → 🤝 Parcerias → 📥 Tenho um convite).\n"
                 "Dá para mudar o acordo até ele aceitar.")
    rows = [[cbtn("⏱ Intervalo mínimo", "ptt", "int", i), cbtn("🔢 Máx. por dia", "ptt", "max", i)],
            [cbtn("⏰ Horário", "ptt", "win", i), cbtn("📆 Fim", "ptt", "end", i)],
            [cbtn(f"{'✅' if p.allow_media else '❌'} Foto/vídeo", "ptt", "media", i),
             cbtn(f"{'✅' if p.allow_buttons else '❌'} Botões", "ptt", "btns", i)],
            [cbtn(f"👥 Meus grupos liberados ({len(mine)})", "ptg", "", i)]]
    if p.status == "draft":
        rows.append([cbtn("📨 Gerar convite", "ptt", "invite", i, style="primary")])
    rows += [[cbtn("🗑 Cancelar proposta", "ptt", "cancel", i)], [cbtn("« Parcerias", "parc")]]
    return text, kb(*rows)


async def screen_active(child: ChildBot, db: Database, manager, settings, p: Partnership) -> Screen:
    other = await bot_row(db, other_id(p, child.id))
    mine = await chosen_chats(db, p, child.id)
    theirs = await chosen_chats(db, p, other.id) if other else []
    sch = await my_schedule(db, p, child.id)
    lines = [f"🤝 <b>Parceria com @{escape(other.username if other else '?')}</b>\n",
             f"Situação: {STATUS[p.status]}" + (" (por você)" if p.status == "paused" and p.paused_by == child.id else
                                                 " (pelo parceiro)" if p.status == "paused" else ""),
             "", "<b>Acordo</b>", terms_text(p), "",
             f"<b>Seus grupos onde o @{escape(other.username if other else '?')} posta</b>"]
    for c in mine:
        inside = await presence(db, p, other.id, c.chat_id) if other else None
        lines.append(f"{'✅' if inside else '❌'} {escape(c.title)}" + ("" if inside else " — falta adicionar o bot dele"))
    if not mine:
        lines.append("<i>nenhum: escolha em 👥 Meus grupos liberados</i>")
    lines.append("\n<b>Grupos dele onde você posta</b>")
    for c in theirs:
        inside = await presence(db, p, child.id, c.chat_id)
        lines.append(f"{'✅' if inside else '⏳'} {escape(c.title)}" + ("" if inside else " — esperando ele adicionar seu bot"))
    if not theirs:
        lines.append("<i>o parceiro ainda não liberou grupos</i>")
    if sch is not None:
        lines.append(f"\n<b>Sua divulgação</b>: {'Ativada ✅' if sch.enabled else 'Desativada ❌'} · "
                     f"próximo: {next_text(sch, settings.tz)} · hoje: "
                     f"{sch.posts_today if sch.posts_day == datetime.now(settings.tz).date().isoformat() else 0}"
                     f"/{p.max_day}" + (f"\n⚠️ {escape(sch.last_error)}" if sch.last_error else ""))
    lines.append("\n• 📣 Minha divulgação: texto, mídias, vários conteúdos e botões que você posta nos grupos dele.\n"
                 "• ➕ Adicionar @parceiro: abre o Telegram para colocar o bot dele nos seus grupos liberados.\n"
                 "• Para mudar o acordo: encerre e mande outro convite.")
    i = p.id
    rows = [[cbtn("📣 Minha divulgação", "sch", "", sch.id if sch else 0)]] if sch else []
    rows.append([cbtn(f"👥 Meus grupos liberados ({len(mine)})", "ptg", "", i)])
    if other:
        rows.append([url(f"➕ Adicionar @{other.username} num grupo", f"https://t.me/{other.username}?{GROUP_ADD}"),
                     url("➕ Num canal", f"https://t.me/{other.username}?{CHANNEL_ADD}")])
    if p.status == "active":
        rows.append([cbtn("⏸ Pausar", "ptt", "pause", i), cbtn("❌ Encerrar", "ptt", "end1", i)])
    else:
        rows.append([cbtn("▶️ Retomar", "ptt", "resume", i), cbtn("❌ Encerrar", "ptt", "end1", i)])
    rows.append([cbtn("« Parcerias", "parc")])
    return "\n".join(lines), kb(*rows)


async def screen_groups(child: ChildBot, db: Database, p: Partnership) -> Screen:
    mine = {c.chat_id for c in await chosen_chats(db, p, child.id)}
    chats = [c for c in await repo.child_chats(db, child.id) if c.bot_status in ("administrator", "creator")]
    text = ("👥 <b>Seus grupos liberados para o parceiro</b>\n\n"
            "Toque para liberar ou tirar. Depois coloque o bot do parceiro nesses grupos "
            "(canal: como administrador com permissão de postar).\n"
            "Se tirar um grupo, o bot do parceiro sai dele.")
    rows = [[cbtn(("✅ " if c.chat_id in mine else "▫️ ") + ("📢 " if c.type == ChatType.CHANNEL else "👥 ")
                  + (c.title or str(c.chat_id))[:40], "ptg", "tog", p.id, j=c.id)] for c in chats]
    if not chats:
        text += "\n\n<i>O bot ainda não é administrador em nenhum grupo seu.</i>"
    return text, kb(*rows, [cbtn("« Voltar", "pt", "", p.id)])


async def screen_detail(child: ChildBot, db: Database, manager, settings, p: Partnership) -> Screen:
    if p.status in ("draft", "invited") and p.a_child_id == child.id:
        return await screen_draft(child, db, manager, p)
    return await screen_active(child, db, manager, settings, p)


def preview_invite(p: Partnership, a: ChildBot) -> str:
    return (f"🤝 <b>Convite de parceria</b>\n\n@{escape(a.username)} quer trocar divulgação com você.\n\n"
            f"<b>Acordo</b>\n{terms_text(p)}\n\n"
            "Aceitando, você escolhe em quais grupos seus ele posta e monta a sua divulgação nos grupos dele.")


# ---------- botões ----------

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
            async with db.session() as s:
                p = Partnership(code=new_code(), a_child_id=child.id, status="draft")
                s.add(p)
                await s.commit()
            await cb.answer()
            return await show(cb, await screen_draft(child, db, manager, p))
        if a == "code":
            return await ask(cb, state, "parc_code", "📥 <b>Tenho um convite</b>\n\nCole o código que o parceiro te "
                             "mandou (ex.: <code>A1B2C3D4</code>).", cbtn("❌ Cancelar", "parc"))
        await cb.answer()
        return await show(cb, await screen_list(child, db, manager))

    p = await get_p(db, i)
    if p is None or child.id not in (p.a_child_id, p.b_child_id) or p.status == "ended":
        await cb.answer("Parceria não encontrada.", show_alert=True)
        return await show(cb, await screen_list(child, db, manager))
    is_a_draft = p.status in ("draft", "invited") and p.a_child_id == child.id

    async def save(**fields):
        async with db.session() as s:
            row = await s.get(Partnership, p.id)
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    note = None
    if s_ == "ptg":
        if a == "tog":
            async with db.session() as s:
                c = await s.get(ChildChat, j)
                if c is None or c.child_id != child.id:
                    await cb.answer("Não encontrado.", show_alert=True)
                    return
                row = await s.scalar(select(PartnerChat).where(PartnerChat.partnership_id == p.id,
                                                               PartnerChat.chat_id == c.chat_id))
                removed = row is not None
                if row:
                    await s.delete(row)
                else:
                    s.add(PartnerChat(partnership_id=p.id, owner_child_id=child.id, chat_id=c.chat_id,
                                      title=c.title or "", type=c.type))
                await s.commit()
            if removed and other_id(p, child.id):
                inside = await presence(db, p, other_id(p, child.id), c.chat_id)
                pbot = manager.bot_for(other_id(p, child.id))
                if inside and pbot is not None:
                    try:
                        await pbot.leave_chat(c.chat_id)
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                    async with db.session() as s:
                        x = await s.get(ChildChat, inside.id)
                        x.bot_status = "left"
                        await s.commit()
            await sync_dests(db, p)
        await cb.answer()
        return await show(cb, await screen_groups(child, db, await get_p(db, p.id)))

    if s_ == "ptt":
        if a in ("int", "max", "win", "end", "media", "btns", "invite", "cancel", "setint", "setmax") and not is_a_draft:
            await cb.answer("O acordo só muda antes de o parceiro aceitar. Para mudar, encerre e mande outro convite.",
                            show_alert=True)
            return
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
            btns = [cbtn(("✅ " if p.max_day == m else "") + str(m), "ptt", "setmax", i, j=m) for m in MAXES]
            return await show(cb, ("🔢 <b>Máximo de posts por dia</b> (em cada grupo)", kb(btns, [cbtn("« Voltar", "pt", "", i)])))
        elif a == "setmax" and j in MAXES:
            await save(max_day=j)
        elif a == "win":
            return await ask(cb, state, "parc_win", "⏰ <b>Horário combinado</b>\n\nEnvie <code>08:00-23:00</code>. "
                             "Envie <code>-</code> para o dia todo.", cbtn("❌ Cancelar", "pt", "", i), pid=i)
        elif a == "end":
            return await ask(cb, state, "parc_end", "📆 <b>Até quando vale</b>\n\nEnvie a data final "
                             "(<code>31/12/2026</code>) ou <code>-</code> para sem data.", cbtn("❌ Cancelar", "pt", "", i), pid=i)
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
            note = "Convite gerado. Mande o link para o parceiro."
        elif a == "cancel":
            await save(status="ended", ended_by=child.id)
            await cb.answer("Proposta cancelada.")
            return await show(cb, await screen_list(child, db, manager))
        elif a == "pause" and p.status == "active":
            await save(status="paused", paused_by=child.id)
            await _tell(manager, await bot_row(db, other_id(p, child.id)),
                        f"⏸ @{escape(child.username)} pausou a parceria. Nenhum dos dois posta até retomar.")
            note = "Pausada: nenhum dos dois posta até retomar."
        elif a == "resume" and p.status == "paused":
            if p.paused_by not in (None, child.id):
                await cb.answer("Quem pausou foi o parceiro: só ele retoma.", show_alert=True)
                return
            await save(status="active", paused_by=None)
            await _tell(manager, await bot_row(db, other_id(p, child.id)), f"▶️ @{escape(child.username)} retomou a parceria.")
            note = "Retomada."
        elif a == "end1":
            await cb.answer()
            return await show(cb, ("❌ <b>Encerrar a parceria?</b>\n\nOs posts param, e o bot de cada um sai dos grupos "
                                   "do outro. Para voltar, é preciso um convite novo.",
                                   kb([cbtn("❌ Sim, encerrar", "ptt", "end2", i, style="danger"),
                                       cbtn("« Voltar", "pt", "", i)])))
        elif a == "end2":
            await end_partnership(db, manager, p, child.id)
            await cb.answer("Parceria encerrada.")
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
        return await screen_active(child, db, manager, settings, await get_p(db, p.id))
    p = await get_p(db, data.get("pid", 0))
    if p is None or p.a_child_id != child.id or p.status not in ("draft", "invited"):
        return "Proposta não encontrada."
    async with db.session() as s:
        row = await s.get(Partnership, p.id)
        if kind == "parc_win":
            if raw == "-":
                row.window_start, row.window_end = 0, 1440
            else:
                import re
                parts = re.split(r"\s*(?:-|até|às|a)\s*", raw)
                a, b = (parse_hhmm(parts[0]), parse_hhmm(parts[1])) if len(parts) == 2 else (None, None)
                if a is None or b is None or a == b:
                    return "Não entendi. Envie assim: <code>08:00-23:00</code> ou /cancelar."
                row.window_start, row.window_end = a, b or 1440
        elif kind == "parc_end":
            if raw == "-":
                row.end_date = None
            else:
                d = parse_date(raw, datetime.now(settings.tz).date())
                if d is None:
                    return "Não entendi. Envie assim: <code>31/12/2026</code> ou /cancelar."
                row.end_date = d.isoformat()
        await s.commit()
    return await screen_draft(child, db, manager, await get_p(db, p.id))


async def on_partner_bot_joined(db: Database, child: ChildBot, p: Partnership, chat_row_id: int) -> None:
    async with db.session() as s:
        row = await s.get(ChildChat, chat_row_id)
        row.partner_of = p.id
        await s.commit()
    await sync_dests(db, p)


async def reschedule_partner(db: Database, sch_id: int, tz) -> None:
    await reschedule(db, sch_id, tz)
