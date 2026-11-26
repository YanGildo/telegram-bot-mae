"""Acervo: tudo que você posta no canal de origem vira post para o bot publicar sozinho."""

import json
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from .. import repo
from ..db import Acervo, AcervoItem, ChildBot, ChildChat, Database, Schedule
from ..ui import CP, Screen, cbtn, kb, show
from .content import (buttons_markup, buttons_text, load_media, media_from_message, media_icon, parse_buttons,
                      plain_len, send_post)
from .flow import ask
from .scheduling import acervo_caption, get_acervo, next_text, to_local

logger = logging.getLogger(__name__)
router = Router(name="acervo")

LIMITS = [25, 50, 100, 200, 500, 0]
CAP_MODES = [("original", "Original"), ("fixed", "Fixa"), ("random", "Sorteada"), ("none", "Nenhuma")]
PAGE = 8


async def counts(db: Database, child_id: int) -> dict:
    async with db.session() as s:
        items = list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == child_id,
                                                               AcervoItem.state.in_(("active", "archived")))))
    act = [x for x in items if x.state == "active"]
    out = {"total": len(act), "fotos": 0, "videos": 0, "albuns": 0, "usadas": 0,
           "arquivadas": sum(1 for x in items if x.state == "archived")}
    for x in act:
        m = load_media(x.media)
        if len(m) > 1:
            out["albuns"] += 1
        elif m and m[0]["t"] == "video":
            out["videos"] += 1
        else:
            out["fotos"] += 1
        out["usadas"] += 1 if x.used_count else 0
    return out


def limit_text(acv: Acervo) -> str:
    if not acv.max_items:
        return "sem limite"
    return f"{acv.max_items} · " + ("rotativo" if acv.full_mode == "rotate" else "travado")


async def source_title(db: Database, child_id: int, chat_id: int | None) -> str:
    if not chat_id:
        return "não definido"
    async with db.session() as s:
        c = await s.scalar(select(ChildChat).where(ChildChat.child_id == child_id, ChildChat.chat_id == chat_id))
    return escape(c.title) if c else str(chat_id)


async def screen_acervo(child: ChildBot, db: Database, settings) -> Screen:
    acv = await get_acervo(db, child.id)
    c = await counts(db, child.id)
    async with db.session() as s:
        schs = list(await s.scalars(select(Schedule).where(Schedule.child_id == child.id, Schedule.kind == "acervo")))
        last = await s.scalar(select(func.max(AcervoItem.last_used_at)).where(AcervoItem.child_id == child.id))
    nexts = [x for x in schs if x.enabled and x.next_run_at]
    nxt = min(nexts, key=lambda x: x.next_run_at) if nexts else None
    order = "aleatória (só repete depois de usar todas)" if acv.order_mode == "random" else \
        "sequencial, " + ("recomeça no fim" if acv.at_end == "restart" else "para no fim")
    text = (f"🗂 <b>Acervo</b>\n\n"
            "Poste fotos, vídeos ou álbuns no <b>canal de origem</b> e o bot guarda sozinho. "
            "As <b>programações</b> dizem quando e onde publicar.\n\n"
            f"📥 Canal de origem: <b>{await source_title(db, child.id, acv.source_chat_id)}</b>\n"
            f"📦 Mídias: <b>{c['total']}</b> (🖼 {c['fotos']} · 🎬 {c['videos']} · 📚 {c['albuns']} álbuns)\n"
            f"✅ Já publicadas: {c['usadas']} · ⏳ Não usadas: {c['total'] - c['usadas']}"
            + (f" · 🗄 Arquivadas: {c['arquivadas']}" if c["arquivadas"] else "") + "\n"
            f"🔢 Limite: {limit_text(acv)}\n"
            f"🔀 Ordem: {order}\n"
            f"⏱ Programações: {len(schs)} ({sum(1 for x in schs if x.enabled)} ativas)\n"
            f"🕑 Última postagem: {to_local(last, settings.tz).strftime('%d/%m %H:%M') if last else '—'}\n"
            f"🚀 Próxima: {next_text(nxt, settings.tz) if nxt else '—'}\n"
            + ("\n⏸ <b>Acervo pausado</b>: nada sai até você retomar.\n" if acv.paused else "")
            + ("\n⚠️ <b>Nenhuma programação ativa</b>: o acervo só publica sozinho depois que você criar "
               "e ativar uma em ⏱ Programações (destinos + de quanto em quanto tempo).\n"
               if not any(x.enabled for x in schs) else "")
            + "\n<b>O que cada botão faz</b>\n"
            "• 📥 Canal de origem: o canal onde você joga as mídias.\n"
            "• 🔢 Limite: quantas mídias ficam na fila e o que fazer quando lotar.\n"
            "• 🔀 Ordem: aleatória ou na ordem em que você postou.\n"
            "• ✍️ Legenda e botões: texto e botões que vão em todo post do acervo.\n"
            "• 📚 Álbuns: <i>inteiros</i> publica o álbum todo junto; <i>soltos</i> publica cada foto/vídeo "
            "como um post separado.\n"
            "• ⏱ Programações: quando e em quais grupos/canais publicar.\n"
            "• 🗂 Ver mídias: lista e remove mídias da fila (sem apagar do canal).\n"
            "• 🧹 Zerar: tira tudo da fila; só contam as mídias postadas depois.")
    return text, kb([cbtn("📥 Canal de origem", "acv", "src")],
                    [cbtn(f"🔢 Limite: {limit_text(acv)}", "acv", "lim"),
                     cbtn("🔀 Ordem: " + ("Aleatória" if acv.order_mode == "random" else "Sequencial"), "acv", "ord")],
                    [cbtn("✍️ Legenda e botões", "acv", "cap"),
                     cbtn("📚 Álbuns: " + ("soltos" if acv.split_albums else "inteiros"), "acv", "alb")],
                    [cbtn(f"⏱ Programações ({len(schs)})", "schs", "acervo")],
                    [cbtn(f"🗂 Ver mídias ({c['total']})", "acv", "items")],
                    [cbtn("🧹 Zerar acervo", "acv", "reset"),
                     cbtn("♻️ Restaurar arquivadas", "acv", "restore") if c["arquivadas"] else None],
                    [cbtn("▶️ Retomar" if acv.paused else "⏸ Pausar", "acv", "pause")],
                    [cbtn("« Painel", "home")])


async def screen_source(child: ChildBot, db: Database) -> Screen:
    acv = await get_acervo(db, child.id)
    chans = [c for c in await repo.child_chats(db, child.id) if c.type == ChatType.CHANNEL]
    text = ("📥 <b>Canal de origem</b>\n\n"
            "1. Crie um canal privado (só seu).\n"
            "2. Coloque este bot como administrador nele (👥 Grupos e canais → ➕ Adicionar a um canal).\n"
            "3. Escolha o canal abaixo.\n\n"
            "Depois é só postar fotos, vídeos ou álbuns lá: cada post entra no acervo sozinho. "
            "O que já estava no canal antes não entra (o Telegram não deixa o bot ler o histórico): "
            "para incluir, encaminhe de novo para o canal.\n\n"
            "Entram fotos, vídeos e arquivos. Para manter a <b>qualidade original</b> (sem a compressão do "
            "Telegram), poste como <b>arquivo</b>. Texto sozinho é ignorado, e a mesma mídia postada duas vezes "
            "só entra uma vez.")
    rows = [[cbtn(("✅ " if c.chat_id == acv.source_chat_id else "▫️ ") + (c.title or str(c.chat_id))[:40],
                  "acv", "setsrc", c.id)] for c in chans]
    if not chans:
        text += "\n\n<i>Nenhum canal com o bot ainda.</i>"
    return text, kb(*rows, [cbtn("« Acervo", "acv")])


async def screen_limit(child: ChildBot, db: Database) -> Screen:
    acv = await get_acervo(db, child.id)
    text = ("🔢 <b>Limite do acervo</b>\n\n"
            "Quantas mídias ficam na fila de publicação.\n\n"
            "• <b>Rotativo</b>: lotou, a mídia nova entra e a mais antiga sai "
            "(ex.: limite 50, a nº 51 entra e a nº 1 sai).\n"
            "• <b>Travado</b>: lotou, as novas não entram até você tirar alguma ou zerar.\n\n"
            "As mídias guardadas são só referências: não pesam no bot.\n\n"
            f"Agora: <b>{limit_text(acv)}</b>")
    nums = [cbtn(("✅ " if acv.max_items == n else "") + (str(n) if n else "Sem limite"), "acv", "setlim", n)
            for n in LIMITS]
    return text, kb(nums[:3], nums[3:],
                    [cbtn(("✅ " if acv.full_mode == "rotate" else "") + "🔄 Rotativo", "acv", "mode", 0),
                     cbtn(("✅ " if acv.full_mode == "lock" else "") + "🔒 Travado", "acv", "mode", 1)],
                    [cbtn("« Acervo", "acv")])


async def screen_order(child: ChildBot, db: Database) -> Screen:
    acv = await get_acervo(db, child.id)
    text = ("🔀 <b>Ordem</b>\n\n"
            "• <b>Aleatória</b>: sorteia, nunca repete a última e só repete depois que todas saíram.\n"
            "• <b>Sequencial</b>: na ordem em que você postou no canal (1, 2, 3…).\n\n"
            "Quando a sequencial chega no fim:\n"
            "• <b>Recomeçar</b>: volta para a primeira.\n"
            "• <b>Parar</b>: não publica mais até chegar mídia nova ou você zerar.")
    return text, kb([cbtn(("✅ " if acv.order_mode == "random" else "") + "🎲 Aleatória", "acv", "setord", 0),
                     cbtn(("✅ " if acv.order_mode == "seq" else "") + "🔢 Sequencial", "acv", "setord", 1)],
                    [cbtn(("✅ " if acv.at_end == "restart" else "") + "🔁 Recomeçar", "acv", "end", 0),
                     cbtn(("✅ " if acv.at_end == "stop" else "") + "⏹ Parar", "acv", "end", 1)],
                    [cbtn("« Acervo", "acv")])


async def screen_caption(child: ChildBot, db: Database) -> Screen:
    acv = await get_acervo(db, child.id)
    rows_btn, _ = parse_buttons(acv.buttons)
    n_random = len([t for t in acv.random_texts.split("\n---\n") if t.strip()]) if acv.random_texts else 0
    text = ("✍️ <b>Legenda e botões do acervo</b>\n\n"
            "Valem para <b>todo</b> post que sair do acervo.\n\n"
            "<b>Legenda</b> (escolha uma):\n"
            "• Original: a legenda que você escreveu no canal.\n"
            "• Fixa: sempre o mesmo texto.\n"
            "• Sorteada: um texto sorteado de uma lista.\n"
            "• Nenhuma: só a mídia.\n\n"
            f"Agora: <b>{dict(CAP_MODES)[acv.caption_mode]}</b>\n"
            f"📝 Texto fixo: {'✅' if acv.fixed_text else '❌'} · 🎲 Textos sorteados: {n_random}\n"
            f"📎 Texto universal (sempre no fim): {'✅' if acv.footer_text else '❌'}\n"
            f"🔠 Botões de link: {sum(len(r) for r in rows_btn) or '❌'}\n"
            f"🖼 Mídia abaixo do texto: {'✅' if acv.caption_above else '❌'}\n\n"
            "• 👀 Ver prévia: o bot te manda aqui um post de exemplo.")
    modes = [cbtn(("✅ " if acv.caption_mode == k else "") + label, "acv", "capm", n) for n, (k, label) in enumerate(CAP_MODES)]
    return text, kb(modes[:2], modes[2:],
                    [cbtn("📝 Texto fixo", "acv", "in", 1), cbtn(f"🎲 Textos sorteados ({n_random})", "acv", "rnd")],
                    [cbtn("📎 Texto universal", "acv", "in", 3), cbtn("🔠 Botões", "acv", "in", 4)],
                    [cbtn(("✅ " if acv.caption_above else "❌ ") + "🖼 Mídia abaixo do texto", "acv", "above")],
                    [cbtn("👀 Ver prévia", "acv", "prev")],
                    [cbtn("« Acervo", "acv")])


def _random_list(acv: Acervo) -> list[str]:
    import re as _re
    return [t.strip() for t in _re.split(r"\n-{3,}\n", acv.random_texts or "") if t.strip()]


async def screen_random(child: ChildBot, db: Database) -> Screen:
    acv = await get_acervo(db, child.id)
    texts_ = _random_list(acv)
    import re as _re
    lines = ["🎲 <b>Textos sorteados</b>\n",
             "Com a legenda em <b>Sorteada</b>, cada post do acervo sai com um destes textos, sorteado.\n"
             "• ➕ Adicionar texto: um texto por vez.\n• 🗑 n: tira o texto.\n"]
    lines += [f"<b>{n}.</b> {escape(_re.sub(r'<[^>]+>', '', t)[:120])}" for n, t in enumerate(texts_, 1)] \
        or ["<i>Nenhum texto ainda.</i>"]
    rows = [[cbtn(f"🗑 {n}", "acv", "rndrm", k) for k, n in list(enumerate(range(1, len(texts_) + 1)))[x:x + 5]]
            for x in range(0, len(texts_), 5)]
    return "\n".join(lines)[:4000], kb([cbtn("➕ Adicionar texto", "acv", "in", 2, style="primary")], *rows,
                                       [cbtn("« Legenda e botões", "acv", "cap")])


async def screen_items(child: ChildBot, db: Database, page: int) -> Screen:
    async with db.session() as s:
        items = list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == child.id,
                                                               AcervoItem.state == "active")
                                     .order_by(AcervoItem.seq.desc())))
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE:(page + 1) * PAGE]
    lines = ["🗂 <b>Mídias do acervo</b>\n",
             "Da mais nova para a mais antiga. 🗑 tira da fila (não apaga do canal).\n"]
    import re as _re
    for x in chunk:
        cap = _re.sub(r"<[^>]+>", "", x.caption_html or "")[:40]
        lines.append(f"{media_icon(load_media(x.media))} <b>#{x.seq}</b> · usada {x.used_count}x"
                     + (f" · {escape(cap)}" if cap else ""))
    if not items:
        lines.append("<i>Vazio. Poste mídias no canal de origem.</i>")
    rows = [[cbtn(f"🗑 #{x.seq}", "acv", "rm", page, j=x.id)] for x in chunk]
    nav = []
    if page > 0:
        nav.append(cbtn("‹ Anterior", "acv", "items", page - 1))
    if page < pages - 1:
        nav.append(cbtn("Próxima ›", "acv", "items", page + 1))
    lines.append(f"\nPágina {page + 1} de {pages}")
    return "\n".join(lines), kb(*rows, nav or None, [cbtn("« Acervo", "acv")])


INPUTS = {
    1: ("acv_fixed", "📝 <b>Texto fixo</b>\n\nEnvie o texto que vai em todo post (pode usar negrito, link, emoji premium).\n"
                     "Envie <code>-</code> para apagar."),
    2: ("acv_random", "🎲 <b>Novo texto sorteado</b>\n\nMande <b>um</b> texto. Ele entra na lista do sorteio "
                      "(para adicionar outro, toque em ➕ de novo)."),
    3: ("acv_footer", "📎 <b>Texto universal</b>\n\nVai no fim de todo post, depois da legenda "
                      "(ex.: chamada para o seu canal). Envie <code>-</code> para apagar."),
    4: ("acv_buttons", "🔠 <b>Botões de link</b>\n\nUm botão por linha, no formato <code>Texto - link</code>. "
                       "Para dois na mesma linha, separe com <code>|</code>.\nExemplo:\n"
                       "<code>Entrar no VIP - https://t.me/seucanal\nSite - seusite.com | Suporte - @seuuser</code>\n\n"
                       "Envie <code>-</code> para apagar."),
}


async def handle_input(kind: str, message: Message, child: ChildBot, db: Database, settings) -> Screen | str:
    raw = (message.text or "").strip()
    value = "" if raw == "-" else (message.html_text or "")
    async with db.session() as s:
        acv = await s.scalar(select(Acervo).where(Acervo.child_id == child.id))
        if kind == "acv_buttons":
            if value:
                rows, errors = parse_buttons(message.text)
                if errors or not rows:
                    return "Não entendi: " + escape(", ".join(errors) or raw) + \
                        "\nUse <code>Texto - link</code>, um por linha. Tente de novo ou /cancelar."
                value = buttons_text(rows)
            acv.buttons = value
        elif kind == "acv_fixed":
            acv.fixed_text = value
        elif kind == "acv_random":
            if not value:
                return "Mande o texto (ou /cancelar)."
            acv.random_texts = "\n---\n".join(_random_list(acv) + [value])
            await s.commit()
            return await screen_random(child, db)
        elif kind == "acv_footer":
            acv.footer_text = value
        await s.commit()
    return await screen_caption(child, db)


@router.callback_query(CP.filter(F.s == "acv"))
async def on_acervo(cb: CallbackQuery, callback_data: CP, state: FSMContext, bot: Bot, child: ChildBot,
                    db: Database, settings) -> None:
    if not await repo.is_child_admin(db, child, cb.from_user.id):
        await cb.answer("Só o dono e os admins do bot usam este painel.", show_alert=True)
        return
    a, i, j = callback_data.a, callback_data.i, callback_data.j
    await state.clear()
    acv = await get_acervo(db, child.id)
    note = None

    async def save(**fields):
        async with db.session() as s:
            row = await s.get(Acervo, acv.id)
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    if a == "src":
        await cb.answer()
        return await show(cb, await screen_source(child, db))
    if a == "alb":
        await cb.answer()
        return await show(cb, (
            "📚 <b>Álbuns</b>\n\n"
            "• <b>Inteiros</b>: o álbum que você postou no canal sai inteiro, num post só.\n"
            "• <b>Soltos</b>: cada foto/vídeo do álbum vira um post separado, com a legenda do álbum. "
            "Rende mais posts com o mesmo material.\n\n"
            "Ao escolher <b>Soltos</b>, os álbuns que já estão no acervo também são quebrados. "
            "Voltar para <b>Inteiros</b> vale só para os álbuns postados depois.\n\n"
            f"Agora: <b>{'Soltos' if acv.split_albums else 'Inteiros'}</b>",
            kb([cbtn(("✅ " if not acv.split_albums else "") + "📚 Inteiros", "acv", "setalb", 0),
                cbtn(("✅ " if acv.split_albums else "") + "🧩 Soltos", "acv", "setalb", 1)],
               [cbtn("« Acervo", "acv")])))
    if a == "setalb":
        if i == 1 and not acv.split_albums:
            await save(split_albums=True)
            n = await split_existing_albums(db, child.id)
            note = f"Álbuns soltos. {n} mídia(s) viraram posts separados." if n else "Álbuns soltos."
        elif i == 0:
            await save(split_albums=False)
            note = "Álbuns inteiros (para os próximos que você postar)."
    if a == "setsrc":
        async with db.session() as s:
            c = await s.get(ChildChat, i)
        if c and c.child_id == child.id:
            await save(source_chat_id=c.chat_id)
            note = f"Canal de origem: {c.title}"
        await cb.answer(note)
        return await show(cb, await screen_acervo(child, db, settings))
    if a in ("lim", "setlim", "mode"):
        if a == "setlim" and i in LIMITS:
            await save(max_items=i, lock_warned=False)
            await _apply_limit(db, child.id)
        elif a == "mode":
            await save(full_mode="rotate" if i == 0 else "lock", lock_warned=False)
        await cb.answer()
        return await show(cb, await screen_limit(child, db))
    if a in ("ord", "setord", "end"):
        if a == "setord":
            await save(order_mode="random" if i == 0 else "seq")
        elif a == "end":
            await save(at_end="restart" if i == 0 else "stop")
        await cb.answer()
        return await show(cb, await screen_order(child, db))
    if a in ("cap", "capm", "above"):
        if a == "capm" and 0 <= i < len(CAP_MODES):
            await save(caption_mode=CAP_MODES[i][0])
        elif a == "above":
            await save(caption_above=not acv.caption_above)
        await cb.answer()
        return await show(cb, await screen_caption(child, db))
    if a in ("rnd", "rndrm"):
        if a == "rndrm":
            texts_ = _random_list(acv)
            if 0 <= i < len(texts_):
                texts_.pop(i)
                await save(random_texts="\n---\n".join(texts_))
        await cb.answer()
        return await show(cb, await screen_random(child, db))
    if a == "in" and i in INPUTS:
        kind, prompt = INPUTS[i]
        return await ask(cb, state, kind, prompt, cbtn("❌ Cancelar", "acv", "cap"))
    if a == "prev":
        async with db.session() as s:
            item = await s.scalar(select(AcervoItem).where(AcervoItem.child_id == child.id,
                                                            AcervoItem.state == "active").order_by(AcervoItem.seq.desc()))
        if item is None:
            await cb.answer("O acervo está vazio. Poste algo no canal de origem.", show_alert=True)
            return
        acv = await get_acervo(db, child.id)
        cap = acervo_caption(acv, item)
        if load_media(item.media) and plain_len(cap) > 1024:
            await cb.answer("Atenção: o texto passa de 1024 caracteres; vai em mensagem separada.", show_alert=True)
        try:
            await send_post(bot, cb.from_user.id, cap, load_media(item.media), buttons_markup(acv.buttons),
                            acv.caption_above)
            await cb.answer("Prévia enviada aqui.")
        except (TelegramBadRequest, TelegramForbiddenError, ValueError) as e:
            await cb.answer(f"Não consegui montar a prévia: {getattr(e, 'message', e)}", show_alert=True)
        return
    if a in ("items", "rm"):
        if a == "rm":
            async with db.session() as s:
                it = await s.get(AcervoItem, j)
                if it and it.child_id == child.id:
                    it.state = "removed"
                    await s.commit()
            await save(lock_warned=False)
            note = "Tirada da fila."
        await cb.answer(note)
        return await show(cb, await screen_items(child, db, i))
    if a == "reset":
        await cb.answer()
        c = await counts(db, child.id)
        return await show(cb, (f"🧹 <b>Zerar o acervo?</b>\n\nAs {c['total']} mídias saem da fila e ficam arquivadas "
                               "(nada é apagado do canal). Daqui pra frente só contam as mídias que você postar "
                               "depois de zerar. Dá para desfazer com ♻️ Restaurar arquivadas.",
                               kb([cbtn("🧹 Sim, zerar", "acv", "reset2", style="danger"), cbtn("« Voltar", "acv")])))
    if a == "reset2":
        async with db.session() as s:
            for it in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == child.id,
                                                                AcervoItem.state == "active")):
                it.state = "archived"
            row = await s.get(Acervo, acv.id)
            row.cursor, row.last_item_id, row.lock_warned = row.seq_counter, None, False
            await s.commit()
        note = "Acervo zerado."
    elif a == "restore":
        async with db.session() as s:
            for it in await s.scalars(select(AcervoItem).where(AcervoItem.child_id == child.id,
                                                                AcervoItem.state == "archived")):
                it.state = "active"
            row = await s.get(Acervo, acv.id)
            row.cursor = 0
            await s.commit()
        await _apply_limit(db, child.id)
        note = "Mídias arquivadas voltaram para a fila."
    elif a == "pause":
        await save(paused=not acv.paused)
        note = "Acervo retomado." if acv.paused else "Acervo pausado."
        if acv.paused:
            async with db.session() as s:
                active = await s.scalar(select(Schedule.id).where(Schedule.child_id == child.id,
                                                                  Schedule.kind == "acervo", Schedule.enabled.is_(True)))
            if active is None:
                await cb.answer("Acervo retomado, mas nada vai sair ainda: crie e ative uma programação em "
                                "⏱ Programações.", show_alert=True)
                return await show(cb, await screen_acervo(child, db, settings))
    await cb.answer(note)
    await show(cb, await screen_acervo(child, db, settings))


async def _apply_limit(db: Database, child_id: int) -> None:
    """Se o limite diminuiu (ou voltaram arquivadas) e está rotativo, tira as mais antigas que sobram."""
    async with db.session() as s:
        acv = await s.scalar(select(Acervo).where(Acervo.child_id == child_id))
        if not acv or not acv.max_items or acv.full_mode != "rotate":
            return
        items = list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == child_id,
                                                               AcervoItem.state == "active").order_by(AcervoItem.seq)))
        for it in items[:max(0, len(items) - acv.max_items)]:
            it.state = "removed"
        await s.commit()


# ---------- captura no canal de origem ----------

@router.channel_post(F.photo | F.video | F.document)
async def capture(message: Message, bot: Bot, child: ChildBot, db: Database) -> None:
    acv = await get_acervo(db, child.id)
    if acv.source_chat_id != message.chat.id:
        return
    m = media_from_message(message)
    if m is None:
        return
    cap = message.html_text if message.caption else ""
    group = message.media_group_id
    async with db.session() as s:
        row = await s.get(Acervo, acv.id)
        # a mesma mídia postada de novo não entra duas vezes
        if m.get("u") and await s.scalar(select(AcervoItem.id).where(
                AcervoItem.child_id == child.id, AcervoItem.state == "active",
                AcervoItem.media.contains(f'"u": "{m["u"]}"'))):
            logger.info("Acervo do @%s: mídia repetida ignorada", child.username)
            return
        siblings = list(await s.scalars(select(AcervoItem).where(
            AcervoItem.child_id == child.id, AcervoItem.media_group_id == group))) if group else []
        if siblings and not row.split_albums:  # mais uma mídia do mesmo álbum
            item = siblings[0]
            media = load_media(item.media)
            if len(media) < 10:
                media.append(m)
                item.media = json.dumps(media)
            if cap and not item.caption_html:
                item.caption_html = cap
            await s.commit()
            return
        if siblings:  # álbum quebrado: cada mídia vira um post, todas com a legenda do álbum
            if cap:
                for sib in siblings:
                    if not sib.caption_html:
                        sib.caption_html = cap
            else:
                cap = next((x.caption_html for x in siblings if x.caption_html), "")
        active = await s.scalar(select(func.count(AcervoItem.id)).where(
            AcervoItem.child_id == child.id, AcervoItem.state == "active")) or 0
        if row.max_items and active >= row.max_items:
            if row.full_mode == "lock":
                warn = not row.lock_warned
                row.lock_warned = True
                await s.commit()
                if warn:
                    await _warn(bot, child, f"🔒 O acervo do @{child.username} está cheio ({row.max_items}) e travado: "
                                            "as mídias novas não estão entrando. Tire algumas, zere ou mude para "
                                            "rotativo em 🗂 Acervo → 🔢 Limite.")
                return
            oldest = list(await s.scalars(select(AcervoItem).where(
                AcervoItem.child_id == child.id, AcervoItem.state == "active").order_by(AcervoItem.seq)
                .limit(active - row.max_items + 1)))
            for o in oldest:
                o.state = "removed"
        row.seq_counter += 1
        s.add(AcervoItem(child_id=child.id, seq=row.seq_counter, media_group_id=group,
                         source_message_id=message.message_id, caption_html=cap, media=json.dumps([m])))
        await s.commit()
        n = row.seq_counter
    logger.info("Acervo do @%s: nova mídia #%s", child.username, n)


async def split_existing_albums(db: Database, child_id: int) -> int:
    """Quebra os álbuns que já estão no acervo em posts soltos, mantendo a ordem."""
    async with db.session() as s:
        acv = await s.scalar(select(Acervo).where(Acervo.child_id == child_id))
        items = list(await s.scalars(select(AcervoItem).where(AcervoItem.child_id == child_id)
                                     .order_by(AcervoItem.seq)))
        seq, created, cursor = 0, 0, acv.cursor
        new_cursor = 0
        for it in items:
            media = load_media(it.media)
            seq += 1
            if it.seq <= cursor:
                new_cursor = seq + (len(media) - 1 if it.state == "active" else 0)
            it.seq = seq
            if it.state != "active" or len(media) < 2:
                continue
            it.media = json.dumps(media[:1])
            for extra in media[1:]:
                seq += 1
                s.add(AcervoItem(child_id=child_id, seq=seq, media_group_id=it.media_group_id,
                                 source_message_id=it.source_message_id, caption_html=it.caption_html,
                                 media=json.dumps([extra]), used_count=it.used_count))
                created += 1
        acv.seq_counter, acv.cursor = seq, new_cursor
        await s.commit()
    await _apply_limit(db, child_id)
    return created


@router.edited_channel_post()
async def capture_edit(message: Message, child: ChildBot, db: Database) -> None:
    """Editou a legenda no canal: atualiza no acervo."""
    async with db.session() as s:
        item = await s.scalar(select(AcervoItem).where(AcervoItem.child_id == child.id,
                                                       AcervoItem.source_message_id == message.message_id))
        if item is not None and message.caption is not None:
            item.caption_html = message.html_text
            await s.commit()


async def _warn(bot: Bot, child: ChildBot, text: str) -> None:
    try:
        await bot.send_message(child.owner_id, text)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
