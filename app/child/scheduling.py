"""Agendador único: programações do acervo e mensagens repetidas.

Cada programação tem cronograma (horário de alinhamento), repetição (a cada X minutos ou a cada
N mensagens no grupo), dias da semana, janela de horário, período e máximo por dia.
O próximo envio é gravado ANTES de postar: se o bot cair no meio, não repete o post.
"""

import asyncio
import json
import logging
import random
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select, update

from ..db import (Acervo, AcervoItem, ChildBot, ChildChat, Database, PendingDelete, Schedule, ScheduleDest,
                  ScheduleItem, utcnow)
from .content import buttons_markup, load_media, random_text, send_post

logger = logging.getLogger(__name__)
_running: set[int] = set()  # programações sendo enviadas agora (evita dois envios ao mesmo tempo)

WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
MIN_INTERVAL = 5  # minutos


# ---------- horários ----------

def hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hhmm(text: str) -> int | None:
    import re
    m = re.fullmatch(r"\s*(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?\s*", (text or "").lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return h * 60 + mi if h < 24 and mi < 60 else None


def parse_date(text: str, today: date) -> date | None:
    import re
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*", text or "")
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (int(y) + (2000 if len(y) == 2 else 0)) if y else today.year
    try:
        return date(year, mo, d)
    except ValueError:
        return None


def to_utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(utc_naive: datetime, tz) -> datetime:
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(tz)


def interval_text(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return "1 dia" if d == 1 else f"{d} dias"
    if minutes % 60 == 0:
        h = minutes // 60
        return "1 hora" if h == 1 else f"{h} horas"
    return f"{minutes} minutos"


def days_text(mask: int) -> str:
    if mask & 127 == 127:
        return "todos os dias"
    if mask & 127 == 0b0011111:
        return "seg a sex"
    return ", ".join(d for n, d in enumerate(WEEKDAYS) if mask & (1 << n)) or "nenhum"


def window_text(sch: Schedule) -> str:
    if sch.window_start == 0 and sch.window_end >= 1440:
        return "dia todo"
    return f"{hhmm(sch.window_start)} às {hhmm(sch.window_end)}"


def allowed_at(sch: Schedule, local: datetime) -> bool:
    if not sch.weekdays & (1 << local.weekday()):
        return False
    minute = local.hour * 60 + local.minute
    ws, we = sch.window_start, sch.window_end
    if not (ws == 0 and we >= 1440):
        inside = ws <= minute < we if ws < we else (minute >= ws or minute < we)
        if not inside:
            return False
    day = local.date().isoformat()
    if sch.start_date and day < sch.start_date:
        return False
    if sch.end_date and day > sch.end_date:
        return False
    return True


def compute_next(sch: Schedule, now_local: datetime) -> datetime | None:
    """Próximo horário (no fuso local) depois de agora, alinhado ao cronograma. None = acabou o período."""
    step = timedelta(minutes=max(MIN_INTERVAL, sch.interval_min))
    start = parse_hhmm(sch.start_time) or 0
    anchor = now_local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    k = (now_local - anchor) // step + 1
    cand = anchor + k * step
    limit = now_local + timedelta(days=400)
    while cand <= limit:
        if sch.end_date and cand.date().isoformat() > sch.end_date:
            return None
        if allowed_at(sch, cand):
            return cand
        if sch.start_date and cand.date().isoformat() < sch.start_date:
            jump = datetime.fromisoformat(sch.start_date).replace(tzinfo=cand.tzinfo)
            cand = anchor + ((jump - anchor) // step) * step
            if cand < jump:
                cand += step
            continue
        cand += step
    return None


def next_text(sch: Schedule, tz) -> str:
    if not sch.enabled:
        return "desativada"
    if sch.trigger == "msgs":
        return f"depois de {sch.every_msgs} mensagens no grupo"
    if sch.next_run_at is None:
        return "período encerrado"
    return to_local(sch.next_run_at, tz).strftime("%d/%m %H:%M")


async def reschedule(db: Database, sch_id: int, tz) -> None:
    async with db.session() as s:
        sch = await s.get(Schedule, sch_id)
        if sch is None:
            return
        if sch.enabled and sch.trigger == "time":
            nxt = compute_next(sch, datetime.now(tz))
            sch.next_run_at = to_utc(nxt) if nxt else None
        else:
            sch.next_run_at = None
        await s.commit()


# ---------- acervo ----------

async def get_acervo(db: Database, child_id: int) -> Acervo:
    async with db.session() as s:
        acv = await s.scalar(select(Acervo).where(Acervo.child_id == child_id))
        if acv is None:
            acv = Acervo(child_id=child_id)
            s.add(acv)
            await s.commit()
        return acv


def acervo_caption(acv: Acervo, item: AcervoItem) -> str:
    base = {"original": item.caption_html, "fixed": acv.fixed_text, "random": random_text(acv.random_texts),
            "none": ""}.get(acv.caption_mode, item.caption_html) or ""
    footer = acv.footer_text or ""
    return "\n\n".join(x for x in (base.strip(), footer.strip()) if x)


def _round_note(items: list, item, seq_mode: bool, at_end: str, what: str) -> str:
    """Texto do aviso quando este envio fecha uma rodada (todas as mídias já saíram)."""
    if len(items) < 2:
        return ""
    if seq_mode:
        if any(x.seq > item.seq for x in items):
            return ""
        tail = "Ele parou: para voltar, mude para recomeçar ou zere." if at_end == "stop" else \
            "O próximo envio recomeça do primeiro."
        return f"🔁 <b>Rodada completa</b> · {what}\n\nTodos os {len(items)} posts já saíram em ordem. {tail}"
    if min(x.used_count for x in items) < item.used_count:
        return ""
    return (f"🔁 <b>Rodada completa</b> · {what}\n\nTodos os {len(items)} posts já saíram "
            f"{item.used_count}x. A partir de agora começa a rodada {item.used_count + 1} (sorteio de novo).")


async def pick_item(db: Database, child_id: int) -> tuple[AcervoItem | None, str]:
    """Escolhe o próximo post do acervo e marca como usado."""
    item, why, _ = await pick_item_round(db, child_id)
    return item, why


async def pick_item_round(db: Database, child_id: int) -> tuple[AcervoItem | None, str, str]:
    async with db.session() as s:
        acv = await s.scalar(select(Acervo).where(Acervo.child_id == child_id))
        if acv is None:
            return None, "acervo vazio", ""
        items = list(await s.scalars(select(AcervoItem).where(
            AcervoItem.child_id == child_id, AcervoItem.state == "active").order_by(AcervoItem.seq)))
        if not items:
            return None, "acervo vazio", ""
        if acv.order_mode == "seq":
            item = next((x for x in items if x.seq > acv.cursor), None)
            if item is None:
                if acv.at_end == "stop":
                    return None, "o acervo chegou ao fim (sequencial, parar no fim)", ""
                item = items[0]
            acv.cursor = item.seq
        else:
            low = min(x.used_count for x in items)  # só repete depois que todas foram usadas
            pool = [x for x in items if x.used_count == low]
            if len(pool) > 1 and acv.last_item_id:
                pool = [x for x in pool if x.id != acv.last_item_id] or pool
            item = random.choice(pool)
        item.used_count += 1
        item.last_used_at = utcnow()
        acv.last_item_id = item.id
        note = _round_note(items, item, acv.order_mode == "seq", acv.at_end, "acervo")
        await s.commit()
        return item, "", note


async def pick_sched_item(db: Database, sch_id: int) -> tuple[ScheduleItem | None, str]:
    """Vários conteúdos de uma mensagem repetida: escolhe o próximo (em ordem ou sorteado)."""
    async with db.session() as s:
        sch = await s.get(Schedule, sch_id)
        items = list(await s.scalars(select(ScheduleItem).where(ScheduleItem.schedule_id == sch_id)
                                     .order_by(ScheduleItem.seq, ScheduleItem.id)))
        if not items:
            return None, ""
        if (sch.content_order or "seq") == "seq":
            item = next((x for x in items if x.seq > (sch.content_cursor or 0)), None) or items[0]
            sch.content_cursor = item.seq
        else:
            low = min(x.used_count for x in items)
            pool = [x for x in items if x.used_count == low]
            if len(pool) > 1 and sch.content_last:
                pool = [x for x in pool if x.id != sch.content_last] or pool
            item = random.choice(pool)
        item.used_count += 1
        sch.content_last = item.id
        note = _round_note(items, item, (sch.content_order or "seq") == "seq", "restart",
                           f"conteúdos de «{sch.name or 'mensagem repetida'}»")
        await s.commit()
        return item, note


# ---------- executar uma programação ----------

async def run_schedule(bot: Bot, db: Database, settings, sch_id: int, only_chat_row: int | None = None,
                       force: bool = False) -> tuple[int, str]:
    if sch_id in _running:
        return 0, "já está enviando"
    _running.add(sch_id)
    try:
        return await _run_schedule(bot, db, settings, sch_id, only_chat_row, force)
    finally:
        _running.discard(sch_id)


async def _run_schedule(bot: Bot, db: Database, settings, sch_id: int, only_chat_row: int | None,
                        force: bool) -> tuple[int, str]:
    tz = settings.tz
    async with db.session() as s:
        sch = await s.get(Schedule, sch_id)
        if sch is None:
            return 0, "programação não encontrada"
        rows = list((await s.execute(select(ScheduleDest, ChildChat).join(
            ChildChat, ChildChat.id == ScheduleDest.chat_row_id).where(ScheduleDest.schedule_id == sch_id))).all())
    dests = [(d, c) for d, c in rows if c.bot_status in ("administrator", "creator", "member")
             and (only_chat_row is None or c.id == only_chat_row)]
    if not dests:
        await _set_error(db, sch_id, "nenhum destino escolhido (ou o bot saiu dos destinos)")
        return 0, "nenhum destino"

    now_local = datetime.now(tz)
    today = now_local.date().isoformat()
    if not force and sch.max_per_day and sch.posts_day == today and sch.posts_today >= sch.max_per_day:
        return 0, "limite do dia atingido"

    round_note = ""
    if sch.kind == "msg":
        text, media, markup, above = sch.text_html, load_media(sch.media), buttons_markup(sch.buttons), sch.caption_above
        variant, round_note = await pick_sched_item(db, sch_id)
        if variant is not None:  # vários conteúdos: usa o da vez (texto dele, ou o texto principal se vazio)
            text, media = variant.text_html or sch.text_html, load_media(variant.media)
        if not text.strip() and not media:
            await _set_error(db, sch_id, "mensagem vazia: use ✍️ Personalizar mensagem")
            return 0, "mensagem vazia"
    else:
        acv = await get_acervo(db, sch.child_id)
        if acv.paused and not force:
            return 0, "acervo pausado"
        item, why, round_note = await pick_item_round(db, sch.child_id)
        if item is None:
            await _set_error(db, sch_id, why)
            if "fim" in why and sch.last_error != why:
                await _notify(bot, db, sch.child_id, f"⏹ <b>Acervo parado</b>\n\n{why}. "
                                                     "Poste mídias novas, zere ou mude para recomeçar.")
            return 0, why
        text, media, markup, above = acervo_caption(acv, item), load_media(item.media), buttons_markup(acv.buttons), \
            acv.caption_above

    sent, errors = 0, []
    for d, c in dests:
        try:
            try:
                ids = await send_post(bot, c.chat_id, text, media, markup, above)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                ids = await send_post(bot, c.chat_id, text, media, markup, above)
        except (TelegramBadRequest, TelegramForbiddenError, ValueError) as e:
            errors.append(f"{c.title}: {getattr(e, 'message', str(e))}")
            continue
        sent += 1
        old = [int(x) for x in d.last_message_ids.split(",") if x]
        if sch.delete_prev and old:
            try:
                await bot.delete_messages(c.chat_id, old)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        if sch.pin and ids:
            try:
                await bot.pin_chat_message(c.chat_id, ids[0], disable_notification=sch.pin == 1)
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                errors.append(f"{c.title}: não fixou ({e.message})")
        async with db.session() as s:
            row = await s.get(ScheduleDest, d.id)
            row.last_message_ids, row.msg_counter = ",".join(map(str, ids)), 0
            if sch.auto_delete_min:
                s.add(PendingDelete(child_id=sch.child_id, chat_id=c.chat_id, message_ids=",".join(map(str, ids)),
                                    due_at=utcnow() + timedelta(minutes=sch.auto_delete_min)))
            await s.commit()
        await asyncio.sleep(0.3)

    async with db.session() as s:
        row = await s.get(Schedule, sch_id)
        if row.posts_day != today:
            row.posts_day, row.posts_today = today, 0
        row.posts_today += 1 if sent else 0
        row.last_run_at = utcnow() if sent else row.last_run_at
        row.last_error = "; ".join(errors)[:300] if errors else None
        await s.commit()
    if round_note and sent:
        await _notify(bot, db, sch.child_id, round_note)
    logger.info("Programação %s: enviado em %s destino(s)%s", sch_id, sent, f", erros: {errors}" if errors else "")
    return sent, ("; ".join(errors) if errors else "")


async def _notify(bot: Bot, db: Database, child_id: int, text: str) -> None:
    """Aviso para o dono: no canal de registros, ou no privado se não tiver canal."""
    from .logs import send_log
    async with db.session() as s:
        child = await s.get(ChildBot, child_id)
    if child is not None:
        await send_log(bot, db, child, text, owner_fallback=True)


async def _set_error(db: Database, sch_id: int, msg: str) -> None:
    async with db.session() as s:
        row = await s.get(Schedule, sch_id)
        if row is not None:
            row.last_error = msg[:300]
            await s.commit()


async def on_group_message(bot: Bot, db: Database, settings, child_id: int, chat_id: int) -> None:
    """Gatilho 'a cada N mensagens': conta as mensagens do grupo e posta quando chega no número."""
    tz = settings.tz
    async with db.session() as s:
        rows = list((await s.execute(select(ScheduleDest, Schedule, ChildChat)
                                     .join(Schedule, Schedule.id == ScheduleDest.schedule_id)
                                     .join(ChildChat, ChildChat.id == ScheduleDest.chat_row_id)
                                     .where(Schedule.child_id == child_id, Schedule.enabled.is_(True),
                                            Schedule.trigger == "msgs", ChildChat.chat_id == chat_id))).all())
        due = []
        for d, sch, c in rows:
            d.msg_counter += 1
            if d.msg_counter >= max(1, sch.every_msgs) and allowed_at(sch, datetime.now(tz)):
                d.msg_counter = 0
                due.append((sch.id, c.id))
        await s.commit()
    for sch_id, chat_row in due:
        await run_schedule(bot, db, settings, sch_id, only_chat_row=chat_row)


# ---------- laço ----------

async def tick(manager, now: datetime | None = None) -> int:
    """Roda as programações vencidas e as exclusões automáticas. Devolve quantas rodou."""
    db, settings = manager.db, manager.settings
    now_utc = now or utcnow()
    async with db.session() as s:
        due = list(await s.scalars(select(Schedule).where(
            Schedule.enabled.is_(True), Schedule.trigger == "time", Schedule.next_run_at.is_not(None),
            Schedule.next_run_at <= now_utc)))
        runnable = []
        for sch in due:
            if manager.bot_for(sch.child_id) is None:
                continue
            nxt = compute_next(sch, to_local(now_utc, settings.tz))  # grava antes de postar
            # só roda quem conseguir "pegar" o horário: se dois processos lerem o mesmo, só um ganha
            res = await s.execute(update(Schedule).where(Schedule.id == sch.id, Schedule.next_run_at == sch.next_run_at)
                                  .values(next_run_at=to_utc(nxt) if nxt else None, enabled=nxt is not None)
                                  .execution_options(synchronize_session=False))
            if res.rowcount == 1:
                runnable.append(sch.id)
        await s.commit()
        deletes = list(await s.scalars(select(PendingDelete).where(PendingDelete.due_at <= now_utc)))
        for p in deletes:
            await s.delete(p)
        await s.commit()
    for p in deletes:
        bot = manager.bot_for(p.child_id)
        if bot is None:
            continue
        try:
            await bot.delete_messages(p.chat_id, [int(x) for x in p.message_ids.split(",") if x])
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    ran = 0
    for sch_id in runnable:
        async with db.session() as s:
            child_id = (await s.get(Schedule, sch_id)).child_id
        bot = manager.bot_for(child_id)
        if bot is not None:
            await run_schedule(bot, db, settings, sch_id)
            ran += 1
    return ran


async def loop(manager) -> None:
    while True:
        try:
            await tick(manager)
        except Exception:
            logger.exception("Erro no agendador")
        await asyncio.sleep(15)


def describe_media(raw: str) -> list[dict]:
    return json.loads(raw or "[]")
