"""Telas das programações: mensagens repetidas e programações do acervo (mesmo motor)."""

import json
import re
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, select

from .. import repo
from ..db import ChildBot, ChildChat, Database, Schedule, ScheduleDest, ScheduleItem
from ..ui import CP, Screen, cbtn, kb, send, show
from .content import (buttons_markup, buttons_text, load_media, media_from_message, media_summary, parse_buttons,
                      send_post)
from .flow import ChildFlow, ask
from .scheduling import (MIN_INTERVAL, WEEKDAYS, days_text, get_acervo, hhmm, interval_text, next_text,
                         parse_date, parse_hhmm, reschedule, run_schedule, window_text)

router = Router(name="programacoes")

INTERVALS = [5, 10, 15, 20, 30, 45, 60, 120, 180, 240, 360, 720, 1440]
MAXES = [0, 1, 2, 3, 5, 10, 24, 48]
AUTODEL = [0, 5, 15, 30, 60, 180, 360, 720, 1440]
KIND_NAME = {"msg": "Mensagem", "acervo": "Programação do acervo", "parc": "Divulgação na parceria"}
PIN_TEXT = {0: "❌", 1: "✅🔕", 2: "✅🔔"}


def label(sch: Schedule, n: int) -> str:
    return sch.name or f"{KIND_NAME[sch.kind]} {n}"


def strip_tags(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t or "")


async def _list(db: Database, child_id: int, kind: str) -> list[Schedule]:
    async with db.session() as s:
        return list(await s.scalars(select(Schedule).where(Schedule.child_id == child_id, Schedule.kind == kind)
                                    .order_by(Schedule.id)))


async def _dest_count(db: Database, sch_id: int) -> int:
    async with db.session() as s:
        return len(list(await s.scalars(select(ScheduleDest.id).where(ScheduleDest.schedule_id == sch_id))))


def repeat_text(sch: Schedule) -> str:
    return f"A cada {sch.every_msgs} mensagens" if sch.trigger == "msgs" else f"A cada {interval_text(sch.interval_min)}"


async def screen_list(child: ChildBot, db: Database, settings, kind: str) -> Screen:
    items = await _list(db, child.id, kind)
    if kind == "msg":
        lines = ["🔁 <b>Mensagens repetidas</b>\n",
                 "Mensagens que o bot manda sozinho nos grupos e canais, de tempos em tempos ou a cada tantas "
                 "mensagens no grupo. Sem limite de quantidade.\n",
                 f"Hora atual: {datetime.now(settings.tz).strftime('%d/%m/%Y %H:%M')}\n"]
    else:
        lines = ["⏱ <b>Programações do acervo</b>\n",
                 "Cada programação publica um post do acervo nos destinos escolhidos, no ritmo que você definir. "
                 "Dá para ter várias (ex.: uma de 30 em 30 min para os grupos e outra 3x por dia para o canal).\n",
                 f"Hora atual: {datetime.now(settings.tz).strftime('%d/%m/%Y %H:%M')}\n"]
    for n, sch in enumerate(items, 1):
        lines.append(f"💭 <b>{n}. {escape(label(sch, n))}</b> · {'Ativado ✅' if sch.enabled else 'Desativado ❌'}")
        lines.append(f"├ Cronograma: {sch.start_time}")
        lines.append(f"├ {repeat_text(sch)}")
        lines.append(f"├ Destinos: {await _dest_count(db, sch.id)} · Próximo: {next_text(sch, settings.tz)}")
        if kind == "msg":
            preview = strip_tags(sch.text_html)[:40] or media_summary(load_media(sch.media))
            lines.append(f"└ {escape(preview) if preview else '(vazia)'}")
        else:
            lines.append("└ " + (f"⚠️ {escape(sch.last_error)}" if sch.last_error else "ok"))
        lines.append("")
    if not items:
        lines.append("<i>Nenhuma ainda. Toque em ➕ Adicionar.</i>\n")
    lines.append("Toque no número para configurar, no ✅/❌ para ligar ou desligar e em 🗑 para excluir.")
    rows = [[cbtn("➕ Adicionar mensagem" if kind == "msg" else "➕ Nova programação", "schs", "add",
                  0 if kind == "acervo" else 1, style="primary")]]
    for n, sch in enumerate(items, 1):
        rows.append([cbtn(f"💭 {n}. {(sch.name or '')[:18]}".strip(), "sch", "", sch.id),
                     cbtn("✅ Ativado" if sch.enabled else "❌ Desativado", "schs", "tog", sch.id),
                     cbtn("🗑", "sch", "del", sch.id)])
    back = cbtn("« Painel", "home") if kind == "msg" else cbtn("« Acervo", "acv")
    return "\n".join(lines), kb(*rows, [back])


async def screen_sch(child: ChildBot, db: Database, settings, sch_id: int) -> Screen | None:
    async with db.session() as s:
        sch = await s.get(Schedule, sch_id)
    if sch is None or sch.child_id != child.id:
        return None
    n = [x.id for x in await _list(db, child.id, sch.kind)].index(sch.id) + 1
    dcount = await _dest_count(db, sch.id)
    period = "sem data" if not (sch.start_date or sch.end_date) else \
        f"{_br(sch.start_date) or 'hoje'} até {_br(sch.end_date) or 'sem fim'}"
    lines = [f"{'🔁' if sch.kind == 'msg' else '🗂'} <b>{escape(label(sch, n))}</b>\n",
             f"💡 Situação: {'Ativado ✅' if sch.enabled else 'Desativado ❌'}",
             f"🕑 Cronograma: {sch.start_time}",
             f"🔁 Repetição: {repeat_text(sch)}",
             f"📅 Dias: {days_text(sch.weekdays)}",
             f"⏰ Horário permitido: {window_text(sch)}",
             f"📆 Período: {period}",
             f"🔢 Máximo por dia: {sch.max_per_day or 'sem limite'}",
             f"📌 Fixar a mensagem: {PIN_TEXT[sch.pin]}",
             f"♻️ Apagar a anterior: {'✅' if sch.delete_prev else '❌'}",
             f"⌛ Exclusão automática: {interval_text(sch.auto_delete_min) if sch.auto_delete_min else '❌'}",
             f"📍 Destinos: {dcount}",
             f"\n🚀 Próximo envio: {next_text(sch, settings.tz)}"]
    if sch.kind == "parc":
        from .partners import get_p, terms_text
        p = await get_p(db, sch.partnership_id or 0)
        if p is not None:
            lines.insert(1, "🤝 <b>Acordo da parceria</b> (o sistema não deixa passar disso)\n" + terms_text(p) + "\n")
    if sch.kind in ("msg", "parc"):
        lines.insert(1, f"📝 Conteúdo: {'texto ✅' if sch.text_html else 'texto ❌'} · "
                        f"{media_summary(load_media(sch.media))} · "
                        f"{'botões ✅' if sch.buttons else 'botões ❌'}")
    if sch.last_error:
        lines.append(f"⚠️ Último aviso: {escape(sch.last_error)}")
    if not dcount:
        lines.append("\n👉 Escolha os destinos em 📍 Destinos antes de ativar.")
    lines.append("\n<b>Ajuda rápida</b>\n"
                 "• Cronograma: o horário de referência. Ex.: 23:23 a cada 30 min → 23:23, 23:53, 00:23…\n"
                 "• Horário permitido: fora dele nada sai (ex.: 08:00 às 23:00).\n"
                 "• Apagar a anterior: ao postar de novo, some a mensagem anterior do mesmo grupo.\n"
                 "• Exclusão automática: apaga cada mensagem depois do tempo escolhido.")
    i = sch.id
    rows = [[cbtn("✏️ Nome", "sch", "name", i)]]
    if sch.kind in ("msg", "parc"):
        rows[0].insert(0, cbtn("✍️ Personalizar mensagem", "msgc", "", i))
    rows += [[cbtn("🕑 Cronograma", "sch", "start", i), cbtn("🔁 Repetição", "sch", "trig", i)],
             [cbtn("📅 Dias da semana", "sch", "days", i), cbtn("⏰ Horário permitido", "sch", "win", i)],
             [cbtn("📆 Período", "sch", "period", i), cbtn("🔢 Máximo por dia", "sch", "max", i)],
             [cbtn(f"📌 Fixar {PIN_TEXT[sch.pin]}", "sch", "pin", i),
              cbtn(f"♻️ Apagar anterior {'✅' if sch.delete_prev else '❌'}", "sch", "delp", i)],
             [cbtn("⌛ Exclusão automática", "sch", "auto", i), cbtn(f"📍 Destinos ({dcount})", "sch", "dst", i)],
             [cbtn("⏸ Desativar" if sch.enabled else "▶️ Ativar", "sch", "tog", i,
                   style=None if sch.enabled else "success"),
              cbtn("🚀 Enviar agora", "sch", "now", i)],
             [cbtn("🗑 Excluir", "sch", "del", i)] if sch.kind != "parc" else None,
             [cbtn("« Parceria", "pt", "", sch.partnership_id) if sch.kind == "parc" else cbtn("« Voltar", "schs", sch.kind)]]
    return "\n".join(lines), kb(*rows)


def _br(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def screen_trigger(sch: Schedule, min_interval: int = 0) -> Screen:
    text = ("🔁 <b>Repetição</b>\n\n"
            "• <b>Tempo</b>: escolha de quanto em quanto tempo (mínimo 5 minutos).\n"
            "• <b>Mensagens</b>: posta depois de tantas mensagens no grupo (bom para não postar em grupo parado).\n\n"
            f"Agora: <b>{repeat_text(sch)}</b>")
    btns = [cbtn(("✅ " if sch.trigger == "time" and sch.interval_min == m else "") + interval_text(m)
                 .replace(" minutos", " min").replace(" horas", "h").replace(" hora", "h"), "sch", "setint", sch.id, j=m)
            for m in INTERVALS if m >= min_interval]
    if min_interval:
        text += f"\n\n🤝 Pela parceria, no mínimo {interval_text(min_interval)}."
    return text, kb(btns[0:4], btns[4:8], btns[8:12], btns[12:],
                    [cbtn("✏️ Outro tempo", "sch", "intc", sch.id),
                     cbtn(("✅ " if sch.trigger == "msgs" else "") + "💬 A cada N mensagens", "sch", "msgs", sch.id)],
                    [cbtn("« Voltar", "sch", "", sch.id)])


def screen_days(sch: Schedule) -> Screen:
    text = "📅 <b>Dias da semana</b>\n\nToque para ligar ou desligar cada dia.\n\nAgora: " + days_text(sch.weekdays)
    btns = [cbtn(("✅ " if sch.weekdays & (1 << n) else "▫️ ") + d, "sch", "day", sch.id, j=n)
            for n, d in enumerate(WEEKDAYS)]
    return text, kb(btns[:4], btns[4:], [cbtn("Todos", "sch", "dayall", sch.id), cbtn("Seg a sex", "sch", "daywk", sch.id)],
                    [cbtn("« Voltar", "sch", "", sch.id)])


def screen_max(sch: Schedule) -> Screen:
    text = ("🔢 <b>Máximo por dia</b>\n\nQuantos envios no máximo por dia (em cada destino). "
            "Bateu o máximo, só volta no dia seguinte.")
    btns = [cbtn(("✅ " if sch.max_per_day == m else "") + (str(m) if m else "Sem limite"), "sch", "setmax", sch.id, j=m)
            for m in MAXES]
    return text, kb(btns[:4], btns[4:], [cbtn("« Voltar", "sch", "", sch.id)])


def screen_auto(sch: Schedule) -> Screen:
    text = ("⌛ <b>Exclusão automática</b>\n\nApaga cada mensagem enviada depois do tempo escolhido. "
            "O bot precisa poder apagar mensagens no grupo/canal.")
    btns = [cbtn(("✅ " if sch.auto_delete_min == m else "") + (interval_text(m) if m else "Desligada"),
                 "sch", "setauto", sch.id, j=m) for m in AUTODEL]
    return text, kb(btns[:3], btns[3:6], btns[6:], [cbtn("« Voltar", "sch", "", sch.id)])


async def screen_dests(child: ChildBot, db: Database, sch: Schedule) -> Screen:
    if sch.kind == "parc":
        async with db.session() as s:
            rows_ = list(await s.scalars(select(ChildChat).where(ChildChat.child_id == child.id,
                                                                 ChildChat.partner_of == sch.partnership_id)))
            chosen = set(await s.scalars(select(ScheduleDest.chat_row_id).where(ScheduleDest.schedule_id == sch.id)))
        text = ("📍 <b>Destinos</b>\n\nOs grupos do parceiro onde o seu bot já entrou (os que ele liberou). "
                "Toque para pausar ou voltar a postar em um deles.")
        if not rows_:
            text += "\n\n<i>O parceiro ainda não colocou o seu bot em nenhum grupo dele.</i>"
        btns = [[cbtn(("✅ " if c.id in chosen else "▫️ ") + (c.title or str(c.chat_id))[:40], "sch", "dsttog", sch.id,
                      j=c.id)] for c in rows_ if c.bot_status in ("administrator", "member", "creator")]
        return text, kb(*btns, [cbtn("« Voltar", "sch", "", sch.id)])
    acv = await get_acervo(db, child.id)
    async with db.session() as s:
        log_chat = (await s.get(ChildBot, child.id)).log_chat_id
    chats = [c for c in await repo.child_chats(db, child.id)
             if c.chat_id not in (acv.source_chat_id, log_chat) and c.bot_status in ("administrator", "creator")]
    async with db.session() as s:
        chosen = set(await s.scalars(select(ScheduleDest.chat_row_id).where(ScheduleDest.schedule_id == sch.id)))
        others = set(await s.scalars(select(ScheduleDest.chat_row_id).join(
            Schedule, Schedule.id == ScheduleDest.schedule_id).where(
            Schedule.child_id == child.id, Schedule.kind == sch.kind, Schedule.enabled.is_(True),
            Schedule.id != sch.id)))
    text = ("📍 <b>Destinos</b>\n\nToque para escolher em quais grupos e canais esta programação publica. "
            "Só aparecem os lugares onde o bot é administrador (o canal de origem do acervo não aparece).")
    if chosen & others:
        text += ("\n\n⚠️ Marcado com 🔁: outra programação ativa também publica ali. Se não quer posts "
                 "em dobro, tire de uma delas.")
    rows = [[cbtn(("✅ " if c.id in chosen else "▫️ ") + ("🔁 " if c.id in others and c.id in chosen else "")
                  + ("🚫 " if c.f_post is False else "") + ("📢 " if c.type == ChatType.CHANNEL else "👥 ")
                  + (c.title or str(c.chat_id))[:40], "sch", "dsttog", sch.id, j=c.id)] for c in chats]
    if any(c.f_post is False for c in chats):
        text += ("\n\n🚫 = 📣 Publicar está desligado nesse grupo (em 👥 Grupos e canais → o grupo): "
                 "mesmo marcado, nada é postado lá.")
    if not chats:
        text += "\n\n<i>Nenhum grupo ou canal ainda. Use 👥 Grupos e canais no painel.</i>"
    return text, kb(*rows, [cbtn("« Voltar", "sch", "", sch.id)])


def _variants(sch: Schedule) -> list[str]:
    try:
        return [t for t in json.loads(sch.text_variants or "[]") if isinstance(t, str) and t.strip()]
    except ValueError:
        return []


async def screen_variants(db: Database, sch: Schedule) -> Screen:
    var = _variants(sch)
    lines = ["📝 <b>Variações de texto</b>\n",
             "Cada envio usa o próximo texto da lista, em ordem (1, 2, 3, 1…). O 1 é o texto principal "
             "(📄 Texto). Conteúdo que tem texto próprio sai sempre com o dele.\n"]
    lines.append(f"<b>1.</b> {escape(strip_tags(sch.text_html)[:120]) or '<i>(sem texto principal)</i>'}")
    for n, t in enumerate(var, 2):
        lines.append(f"<b>{n}.</b> {escape(strip_tags(t)[:120])}")
    lines.append("\n• ➕ Adicionar variação: mande o texto (formatação e emoji premium ficam).\n• 🗑 tira a variação.")
    rows = [[cbtn("➕ Adicionar variação", "msgv", "add", sch.id, style="primary")]]
    rows += [[cbtn(f"🗑 {n}", "msgv", "rm", sch.id, j=k) for k, n in list(enumerate(range(2, len(var) + 2)))[x:x + 5]]
             for x in range(0, len(var), 5)]
    return "\n".join(lines)[:4000], kb(*rows, [cbtn("« Voltar", "msgc", "", sch.id)])


@router.callback_query(CP.filter(F.s == "msgv"))
async def on_variants(cb: CallbackQuery, callback_data: CP, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    a, i, j = callback_data.a, callback_data.i, callback_data.j
    sch = await _get(db, child, i)
    if sch is None:
        await cb.answer("Não encontrada.", show_alert=True)
        return
    await state.clear()
    if a == "add":
        return await ask(cb, state, "msg_var", "📝 <b>Nova variação de texto</b>\n\nMande o texto. Ele entra no "
                         "rodízio junto com o texto principal.", cbtn("❌ Cancelar", "msgv", "", i), sch_id=i)
    if a == "rm":
        var = _variants(sch)
        if 0 <= j < len(var):
            var.pop(j)
            await _update(db, i, text_variants=json.dumps(var))
    await cb.answer()
    await show(cb, await screen_variants(db, await _get(db, child, i)))


async def _items(db: Database, sch_id: int) -> list[ScheduleItem]:
    async with db.session() as s:
        return list(await s.scalars(select(ScheduleItem).where(ScheduleItem.schedule_id == sch_id)
                                    .order_by(ScheduleItem.seq, ScheduleItem.id)))


async def screen_content(db: Database, sch: Schedule) -> Screen:
    media = load_media(sch.media)
    rows_btn, _ = parse_buttons(sch.buttons)
    n_items = len(await _items(db, sch.id))
    n_var = len(_variants(sch))
    text = ("✍️ <b>Personalizar mensagem</b>\n\n"
            f"📄 Texto {'✅' if sch.text_html else '❌'}" + (f" + {n_var} variação(ões)" if n_var else "") + "\n"
            + (f"📸 Mídias: usa as do 📚 Vários conteúdos ({n_items})\n" if n_items else
               f"📸 Mídias {'✅ ' + media_summary(media) if media else '❌'}\n")
            + 
            f"🔠 Botões de link {'✅' if rows_btn else '❌'}\n"
            f"🖼 Mídia abaixo do texto {'✅' if sch.caption_above else '❌'}\n"
            f"📚 Vários conteúdos: {n_items or '❌'}\n\n"
            "👉 Use os botões abaixo para definir cada parte. 👀 Veja mostra só aquela parte; "
            "👀 Visualização completa manda a mensagem inteira aqui, do jeito que vai sair.\n\n"
            "📝 <b>Variações de texto</b>: outras versões da copy. Cada envio usa a próxima (1, 2, 3, 1…).\n"
            "📚 <b>Vários conteúdos</b>: vários posts (mídia, com ou sem texto) e a programação usa um por vez.\n"
            "Conteúdo <b>com texto próprio</b> (ex.: vídeo de prova social) sai sempre com o texto dele; "
            "conteúdo <b>sem texto</b> sai com a próxima variação.")
    i = sch.id
    return text, kb([cbtn(f"📝 Variações de texto ({n_var + (1 if sch.text_html else 0)})", "msgv", "", i),
                     cbtn(f"📚 Vários conteúdos ({n_items})", "msgi", "", i)],
                    [cbtn("📄 Texto", "msgc", "text", i), cbtn("👀 Veja", "msgc", "vt", i)],
                    [cbtn("📸 Mídias", "msgc", "media", i), cbtn("👀 Veja", "msgc", "vm", i)],
                    [cbtn("🔠 Botões de URL", "msgc", "btn", i), cbtn("👀 Veja", "msgc", "vb", i)],
                    [cbtn(("✅ " if sch.caption_above else "❌ ") + "🖼 Mídia abaixo do texto", "msgc", "above", i)],
                    [cbtn("👀 Visualização completa", "msgc", "full", i)],
                    [cbtn("« Voltar", "sch", "", i)])


async def _get(db: Database, child: ChildBot, sch_id: int) -> Schedule | None:
    async with db.session() as s:
        sch = await s.get(Schedule, sch_id)
    return sch if sch is not None and sch.child_id == child.id else None


async def _update(db: Database, sch_id: int, **fields) -> None:
    async with db.session() as s:
        row = await s.get(Schedule, sch_id)
        for k, v in fields.items():
            setattr(row, k, v)
        await s.commit()


# ---------- botões ----------

async def screen_items(db: Database, sch: Schedule) -> Screen:
    items = await _items(db, sch.id)
    order = "em ordem" if (sch.content_order or "seq") == "seq" else "sorteado (só repete depois de usar todos)"
    lines = [f"📚 <b>Vários conteúdos</b> · {escape(sch.name or 'mensagem repetida')}\n",
             "Cada envio da programação usa <b>um</b> destes posts, " + order + ". "
             "Os botões de link, destinos e horários são os da programação. "
             "Conteúdo com 💬 texto próprio sai sempre com ele; sem texto, sai com a próxima variação de texto.\n",
             "Para adicionar: toque em ➕ e mande (ou encaminhe de qualquer grupo/canal) as mensagens aqui: "
             "cada mensagem ou álbum vira um conteúdo. Nada disso entra no acervo.\n"]
    for n, it in enumerate(items[:30], 1):
        prev = strip_tags(it.text_html)[:40]
        lines.append(f"{n}. {media_summary(load_media(it.media))}" + (f" · 💬 {escape(prev)}" if prev else " · texto da vez")
                     + f" · usado {it.used_count}x")
    if not items:
        lines.append("<i>Nenhum ainda: a programação usa a mensagem principal.</i>")
    if len(items) > 30:
        lines.append(f"… e mais {len(items) - 30}")
    rows = [[cbtn("➕ Adicionar conteúdos", "msgi", "add", sch.id, style="primary")],
            [cbtn(("✅ " if (sch.content_order or "seq") == "seq" else "") + "🔢 Em ordem", "msgi", "ord", sch.id, j=0),
             cbtn(("✅ " if sch.content_order == "random" else "") + "🎲 Sorteado", "msgi", "ord", sch.id, j=1)]]
    pairs = list(enumerate(items[:30], 1))
    rows += [[b for n, it in pairs[k:k + 3] for b in (cbtn(f"✏️ {n}", "msgi", "txt", sch.id, j=it.id),
                                                      cbtn(f"🗑 {n}", "msgi", "rm", sch.id, j=it.id))]
             for k in range(0, len(pairs), 3)]
    if items:
        lines.append("\n✏️ n: dá (ou tira) um texto próprio para aquele conteúdo.")
    if items:
        rows.append([cbtn("🧹 Apagar todos", "msgi", "clr", sch.id)])
    return "\n".join(lines), kb(*rows, [cbtn("« Voltar", "msgc", "", sch.id)])


@router.callback_query(CP.filter(F.s == "msgi"))
async def on_items(cb: CallbackQuery, callback_data: CP, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    a, i, j = callback_data.a, callback_data.i, callback_data.j
    sch = await _get(db, child, i)
    if sch is None:
        await cb.answer("Não encontrada.", show_alert=True)
        return
    await state.clear()
    if a == "add":
        await state.set_state(ChildFlow.items)
        await state.update_data(sch_id=i)
        await cb.answer()
        return await show(cb, ("➕ <b>Adicionar conteúdos</b>\n\nMande ou encaminhe as mensagens agora: texto, foto, vídeo, "
                               "arquivo ou álbum (com legenda). Cada mensagem ou álbum vira um conteúdo.\n"
                               "Quando terminar, toque em ✅ Pronto.", kb([cbtn("✅ Pronto", "msgi", "", i)])))
    if a == "txt":
        return await ask(cb, state, "item_text", "✏️ <b>Texto próprio deste conteúdo</b>\n\nMande o texto que deve "
                         "sair sempre com ele (ex.: a copy da prova social). Envie <code>-</code> para tirar e ele "
                         "passar a usar as variações.", cbtn("❌ Cancelar", "msgi", "", i), sch_id=i, item_id=j)
    if a == "ord":
        await _update(db, i, content_order="seq" if j == 0 else "random")
    elif a == "rm":
        async with db.session() as s:
            await s.execute(delete(ScheduleItem).where(ScheduleItem.id == j, ScheduleItem.schedule_id == i))
            await s.commit()
    elif a == "clr":
        async with db.session() as s:
            await s.execute(delete(ScheduleItem).where(ScheduleItem.schedule_id == i))
            await s.commit()
        await _update(db, i, content_cursor=0, content_last=None)
    await cb.answer()
    await show(cb, await screen_items(db, await _get(db, child, i)))


@router.message(ChildFlow.items, F.chat.type == ChatType.PRIVATE)
async def receive_item(message: Message, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        return
    sch = await _get(db, child, (await state.get_data()).get("sch_id", 0))
    if sch is None:
        await state.clear()
        return
    m = media_from_message(message)
    text = message.html_text if (message.text or message.caption) else ""
    if m is None and not text:
        await message.answer("Esse tipo de mensagem não serve. Mande texto, foto, vídeo, arquivo ou álbum.")
        return
    group = message.media_group_id
    async with db.session() as s:
        last = await s.scalar(select(ScheduleItem).where(ScheduleItem.schedule_id == sch.id)
                              .order_by(ScheduleItem.seq.desc(), ScheduleItem.id.desc()).limit(1))
        if group and last is not None and last.media_group_id == group:  # resto do mesmo álbum
            media = load_media(last.media)
            if m and len(media) < 10:
                media.append(m)
                last.media = json.dumps(media)
            if text and not last.text_html:
                last.text_html = text
            await s.commit()
            return
        s.add(ScheduleItem(schedule_id=sch.id, seq=(last.seq + 1) if last else 1, text_html=text,
                           media=json.dumps([m] if m else []), media_group_id=group))
        await s.commit()
        total = len(list(await s.scalars(select(ScheduleItem.id).where(ScheduleItem.schedule_id == sch.id))))
    await message.answer(f"✅ Conteúdo {total} salvo. Mande o próximo ou toque em ✅ Pronto.",
                         reply_markup=kb([cbtn("✅ Pronto", "msgi", "", sch.id)]))


@router.callback_query(CP.filter(F.s.in_({"schs", "sch", "msgc"})))
async def on_schedule(cb: CallbackQuery, callback_data: CP, state: FSMContext, bot: Bot, child: ChildBot,
                      db: Database, settings) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    s_, a, i, j = callback_data.s, callback_data.a, callback_data.i, callback_data.j
    tz = settings.tz
    await state.clear()

    if s_ == "schs":
        if a == "add":
            kind = "acervo" if i == 0 else "msg"
            async with db.session() as s:
                row = Schedule(child_id=child.id, kind=kind, start_time=datetime.now(tz).strftime("%H:%M"))
                s.add(row)
                await s.commit()
                new_id = row.id
            await cb.answer("Criada. Configure e depois ative.")
            return await show(cb, await screen_sch(child, db, settings, new_id))
        if a == "tog":
            sch = await _get(db, child, i)
            if sch:
                if not sch.enabled and not await _dest_count(db, sch.id):
                    await cb.answer("Escolha os destinos antes de ativar (abra a programação → 📍 Destinos).",
                                    show_alert=True)
                    return
                await _update(db, i, enabled=not sch.enabled)
                await reschedule(db, i, tz)
            await cb.answer()
            return await show(cb, await screen_list(child, db, settings, sch.kind if sch else "msg"))
        await cb.answer()
        return await show(cb, await screen_list(child, db, settings, a if a in ("msg", "acervo") else "msg"))

    sch = await _get(db, child, i)
    if sch is None:
        await cb.answer("Não encontrada.", show_alert=True)
        return

    if s_ == "msgc":
        if a == "text":
            return await ask(cb, state, "msg_text", "📄 <b>Texto da mensagem</b>\n\nEnvie o texto (negrito, links, "
                             "emoji premium e formatação ficam como você mandar). Envie <code>-</code> para apagar.",
                             cbtn("❌ Cancelar", "msgc", "", i), sch_id=i)
        if a == "btn":
            return await ask(cb, state, "msg_btn", "🔠 <b>Botões de link</b>\n\nUm por linha: <code>Texto - link</code>. "
                             "Dois na mesma linha: separe com <code>|</code>.\nExemplo:\n"
                             "<code>Entrar - https://t.me/seugrupo | Site - seusite.com</code>\n\n"
                             "Envie <code>-</code> para apagar.", cbtn("❌ Cancelar", "msgc", "", i), sch_id=i)
        if a == "media":
            await state.set_state(ChildFlow.media)
            await state.update_data(sch_id=i)
            await cb.answer()
            return await show(cb, ("📸 <b>Mídias</b>\n\nEnvie uma foto, um vídeo ou um álbum (até 10). "
                                   "As novas substituem as antigas.\nQuando terminar, toque em ✅ Pronto.",
                                   kb([cbtn("🗑 Sem mídia", "msgc", "clrmedia", i)], [cbtn("✅ Pronto", "msgc", "", i)])))
        if a == "clrmedia":
            await _update(db, i, media="[]")
            await cb.answer("Mídias removidas.")
        elif a == "above":
            await _update(db, i, caption_above=not sch.caption_above)
            await cb.answer()
        elif a in ("vt", "vm", "vb", "full"):
            text = sch.text_html if a in ("vt", "full") else ""
            media = load_media(sch.media) if a in ("vm", "full") else []
            items = await _items(db, i)
            if a in ("vm", "full") and items:  # com vários conteúdos, mostra o próximo da lista (sem gastar a vez)
                if (sch.content_order or "seq") == "seq":
                    nxt = next((x for x in items if x.seq > (sch.content_cursor or 0)), items[0])
                else:
                    nxt = min(items, key=lambda x: x.used_count)
                media = load_media(nxt.media)
                if a == "full" and nxt.text_html:
                    text = nxt.text_html
                elif a == "full":
                    from .scheduling import text_pool
                    pool = text_pool(sch)
                    text = pool[(sch.text_cursor or 0) % len(pool)] if pool else ""
            markup = buttons_markup(sch.buttons) if a in ("vb", "full") else None
            if a == "vb" and markup is not None:
                text = "🔠 Botões:"
            try:
                await send_post(bot, cb.from_user.id, text, media, markup, sch.caption_above)
                await cb.answer(f"Enviei aqui embaixo: o próximo dos {len(items)} conteúdos." if items and a != "vb"
                                else "Enviei aqui embaixo.")
            except ValueError:
                await cb.answer("Essa parte está vazia.", show_alert=True)
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                await cb.answer(f"Não consegui mostrar: {e.message}", show_alert=True)
            return
        else:
            await cb.answer()
        return await show(cb, await screen_content(db, await _get(db, child, i)))

    # ---- s_ == "sch"
    if a == "name":
        return await ask(cb, state, "sch_name", "✏️ <b>Nome da programação</b>\n\nEnvie um nome curto para saber do "
                         "que se trata (ex.: <i>Divulgação VIP noite</i>). Envie <code>-</code> para tirar o nome.",
                         cbtn("❌ Cancelar", "sch", "", i), sch_id=i)
    if a == "start":
        return await ask(cb, state, "sch_start", "🕑 <b>Cronograma</b>\n\nEnvie o horário de referência, ex.: "
                         "<code>23:23</code>. Os envios saem nesse horário e de tanto em tanto a partir dele.",
                         cbtn("❌ Cancelar", "sch", "", i), sch_id=i)
    pmin, pmax = 0, 0
    if sch.kind == "parc":
        from .partners import get_p
        p = await get_p(db, sch.partnership_id or 0)
        pmin, pmax = (p.min_interval, p.max_day) if p else (0, 0)
    if a == "trig":
        await cb.answer()
        return await show(cb, screen_trigger(sch, pmin))
    if a == "setint" and j >= MIN_INTERVAL:
        if j < pmin:
            await cb.answer(f"O acordo da parceria é de no mínimo {interval_text(pmin)}.", show_alert=True)
            return
        await _update(db, i, interval_min=j, trigger="time")
        await reschedule(db, i, tz)
        await cb.answer(f"A cada {interval_text(j)}.")
        return await show(cb, screen_trigger(await _get(db, child, i), pmin))
    if a == "intc":
        return await ask(cb, state, "sch_int", "✏️ <b>Outro tempo</b>\n\nEnvie em minutos (ex.: <code>90</code>) "
                         "ou em horas com h (ex.: <code>8h</code>). Mínimo 5 minutos.",
                         cbtn("❌ Cancelar", "sch", "trig", i), sch_id=i)
    if a == "msgs":
        return await ask(cb, state, "sch_msgs", "💬 <b>A cada N mensagens</b>\n\nEnvie o número de mensagens no grupo "
                         "entre um envio e outro (ex.: <code>50</code>). Vale só para grupos.",
                         cbtn("❌ Cancelar", "sch", "trig", i), sch_id=i)
    if a in ("days", "day", "dayall", "daywk"):
        if a == "day" and 0 <= j < 7:
            mask = sch.weekdays ^ (1 << j)
            await _update(db, i, weekdays=mask or sch.weekdays)
        elif a == "dayall":
            await _update(db, i, weekdays=127)
        elif a == "daywk":
            await _update(db, i, weekdays=0b0011111)
        if a != "days":
            await reschedule(db, i, tz)
        await cb.answer()
        return await show(cb, screen_days(await _get(db, child, i)))
    if a == "win":
        return await ask(cb, state, "sch_win", "⏰ <b>Horário permitido</b>\n\nEnvie no formato "
                         "<code>08:00-23:00</code>. Fora desse horário nada sai. "
                         "Envie <code>-</code> para o dia todo.", cbtn("❌ Cancelar", "sch", "", i), sch_id=i)
    if a == "period":
        return await ask(cb, state, "sch_period", "📆 <b>Período</b>\n\nEnvie a data de início e a final: "
                         "<code>20/11/2026 - 31/12/2026</code>. Só a final: <code>- 31/12/2026</code>. "
                         "Envie <code>-</code> para tirar as datas.", cbtn("❌ Cancelar", "sch", "", i), sch_id=i)
    if a in ("max", "setmax"):
        if a == "setmax" and j in MAXES:
            if pmax and (j == 0 or j > pmax):
                await cb.answer(f"O acordo da parceria é de até {pmax} por dia.", show_alert=True)
                return
            await _update(db, i, max_per_day=j)
        await cb.answer()
        return await show(cb, screen_max(await _get(db, child, i)))
    if a in ("auto", "setauto"):
        if a == "setauto" and j in AUTODEL:
            await _update(db, i, auto_delete_min=j)
        await cb.answer()
        return await show(cb, screen_auto(await _get(db, child, i)))
    if a in ("dst", "dsttog"):
        if a == "dsttog":
            async with db.session() as s:
                c = await s.get(ChildChat, j)
                if c and c.child_id == child.id:
                    row = await s.scalar(select(ScheduleDest).where(ScheduleDest.schedule_id == i,
                                                                    ScheduleDest.chat_row_id == j))
                    if row:
                        await s.delete(row)
                    else:
                        s.add(ScheduleDest(schedule_id=i, chat_row_id=j))
                    await s.commit()
        await cb.answer()
        return await show(cb, await screen_dests(child, db, sch))
    if a in ("del", "del2") and sch.kind == "parc":
        await cb.answer("A divulgação da parceria não se exclui: desative ou encerre a parceria.", show_alert=True)
        return
    if a == "del":
        await cb.answer()
        return await show(cb, ("🗑 <b>Excluir esta programação?</b>",
                               kb([cbtn("🗑 Sim, excluir", "sch", "del2", i, style="danger"),
                                   cbtn("« Voltar", "sch", "", i)])))
    if a == "del2":
        async with db.session() as s:
            await s.execute(delete(ScheduleDest).where(ScheduleDest.schedule_id == i))
            await s.execute(delete(Schedule).where(Schedule.id == i))
            await s.commit()
        await cb.answer("Excluída.")
        return await show(cb, await screen_list(child, db, settings, sch.kind))
    if a == "pin":
        await _update(db, i, pin=(sch.pin + 1) % 3)
        await cb.answer({0: "Não fixa.", 1: "Fixa sem notificar.", 2: "Fixa notificando."}[(sch.pin + 1) % 3])
    elif a == "delp":
        await _update(db, i, delete_prev=not sch.delete_prev)
        await cb.answer()
    elif a == "tog":
        if not sch.enabled and not await _dest_count(db, i):
            await cb.answer("Escolha os destinos antes de ativar (📍 Destinos).", show_alert=True)
            return
        if not sch.enabled and sch.kind in ("msg", "parc") and not sch.text_html and not load_media(sch.media) \
                and not await _items(db, i):
            await cb.answer("A mensagem está vazia. Use ✍️ Personalizar mensagem.", show_alert=True)
            return
        await _update(db, i, enabled=not sch.enabled)
        await reschedule(db, i, tz)
        await cb.answer("Desativada." if sch.enabled else "Ativada.")
    elif a == "now":
        await cb.answer("Enviando…")
        sent, err = await run_schedule(bot, db, settings, i, force=True)
        try:
            await cb.message.answer(f"🚀 Enviado em {sent} destino(s)." + (f"\n⚠️ {escape(err)}" if err else ""))
        except TelegramBadRequest:
            pass
    else:
        await cb.answer()
    screen = await screen_sch(child, db, settings, i)
    if screen:
        await show(cb, screen)


# ---------- textos digitados ----------

async def handle_input(kind: str, data: dict, message: Message, child: ChildBot, db: Database, settings) -> Screen | str:
    sch = await _get(db, child, data.get("sch_id", 0))
    if sch is None:
        return "Programação não encontrada."
    raw = (message.text or "").strip()
    tz = settings.tz
    if kind == "msg_text":
        await _update(db, sch.id, text_html="" if raw == "-" else (message.html_text or ""))
        return await screen_content(db, await _get(db, child, sch.id))
    if kind == "msg_var":
        if not raw:
            return "Mande o texto da variação ou /cancelar."
        await _update(db, sch.id, text_variants=json.dumps(_variants(sch) + [message.html_text]))
        return await screen_variants(db, await _get(db, child, sch.id))
    if kind == "item_text":
        async with db.session() as s:
            it = await s.get(ScheduleItem, data.get("item_id", 0))
            if it is None or it.schedule_id != sch.id:
                return "Conteúdo não encontrado."
            it.text_html = "" if raw == "-" else (message.html_text or "")
            await s.commit()
        return await screen_items(db, await _get(db, child, sch.id))
    if kind == "msg_btn":
        if raw == "-":
            value = ""
        else:
            rows, errors = parse_buttons(message.text)
            if errors or not rows:
                return "Não entendi: " + escape(", ".join(errors) or raw) + \
                    "\nUse <code>Texto - link</code>, um por linha. Tente de novo ou /cancelar."
            value = buttons_text(rows)
        await _update(db, sch.id, buttons=value)
        return await screen_content(db, await _get(db, child, sch.id))
    if kind == "sch_name":
        await _update(db, sch.id, name=None if raw == "-" else raw[:60])
        return await screen_sch(child, db, settings, sch.id)
    if kind == "sch_start":
        m = parse_hhmm(raw)
        if m is None:
            return "Não entendi. Envie assim: <code>23:23</code> ou /cancelar."
        await _update(db, sch.id, start_time=hhmm(m))
    elif kind == "sch_int":
        mh = re.fullmatch(r"(\d+)\s*h", raw.lower())
        minutes = int(mh.group(1)) * 60 if mh else (int(raw) if raw.isdigit() else 0)
        if minutes < 5 or minutes > 10080:
            return "Envie de 5 minutos a 7 dias (ex.: <code>90</code> ou <code>8h</code>) ou /cancelar."
        if sch.kind == "parc":
            from .partners import get_p
            p = await get_p(db, sch.partnership_id or 0)
            if p and minutes < p.min_interval:
                return f"O acordo da parceria é de no mínimo {interval_text(p.min_interval)}. Envie um tempo maior ou /cancelar."
        await _update(db, sch.id, interval_min=minutes, trigger="time")
    elif kind == "sch_msgs":
        if not raw.isdigit() or not 1 <= int(raw) <= 10000:
            return "Envie um número de 1 a 10000 ou /cancelar."
        await _update(db, sch.id, every_msgs=int(raw), trigger="msgs")
    elif kind == "sch_win":
        if raw == "-":
            await _update(db, sch.id, window_start=0, window_end=1440)
        else:
            parts = re.split(r"\s*(?:-|até|às|a)\s*", raw)
            a, b = (parse_hhmm(parts[0]), parse_hhmm(parts[1])) if len(parts) == 2 else (None, None)
            if a is None or b is None or a == b:
                return "Não entendi. Envie assim: <code>08:00-23:00</code> ou /cancelar."
            await _update(db, sch.id, window_start=a, window_end=b if b else 1440)
    elif kind == "sch_period":
        today = datetime.now(tz).date()
        if raw == "-":
            await _update(db, sch.id, start_date=None, end_date=None)
        else:
            parts = [p.strip() for p in raw.split("-", 1)] if "-" in raw else [raw, ""]
            start = parse_date(parts[0], today) if parts[0] else None
            end = parse_date(parts[1], today) if len(parts) > 1 and parts[1] else None
            if (parts[0] and start is None) or (len(parts) > 1 and parts[1] and end is None) or (start and end and end < start):
                return "Não entendi. Envie assim: <code>20/11/2026 - 31/12/2026</code> ou /cancelar."
            await _update(db, sch.id, start_date=start.isoformat() if start else None,
                          end_date=end.isoformat() if end else None)
    await reschedule(db, sch.id, tz)
    return await screen_sch(child, db, settings, sch.id)


@router.message(ChildFlow.media, F.chat.type == ChatType.PRIVATE, F.photo | F.video)
async def receive_media(message: Message, state: FSMContext, child: ChildBot, db: Database) -> None:
    if not await repo.is_child_admin(db, child, message.from_user.id):
        return
    data = await state.get_data()
    sch = await _get(db, child, data.get("sch_id", 0))
    if sch is None:
        await state.clear()
        return
    m = media_from_message(message)
    group = message.media_group_id
    if group and data.get("group") == group:
        media = load_media(sch.media)
        if len(media) < 10:
            media.append(m)
        await _update(db, sch.id, media=json.dumps(media))
        return
    await state.update_data(group=group)
    await _update(db, sch.id, media=json.dumps([m]))  # nova mensagem: substitui as anteriores
    if message.caption and not sch.text_html:
        await _update(db, sch.id, text_html=message.html_text)
    await message.answer("✅ Mídia recebida" + (" (álbum)" if group else "") + ". Mande outra para trocar ou toque em ✅ Pronto.",
                         reply_markup=kb([cbtn("✅ Pronto", "msgc", "", sch.id)]))
