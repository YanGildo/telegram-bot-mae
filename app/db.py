"""Banco de dados: tabelas e conexão.

SQLite (um arquivo em dados/mae.db), sem instalação. Para PostgreSQL, basta mudar DATABASE_URL no .env.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
                        delete, inspect)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Hora atual em UTC, sem fuso (padrão do banco)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Pessoa que usou o bot mãe."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)  # liberado para criar bots (fase fechada)
    platform_admin: Mapped[bool | None] = mapped_column(Boolean, default=False)  # admin do bot mãe (dado pelo dono)
    referrer: Mapped[str | None] = mapped_column(String(64))  # de onde veio (ex.: parc_12)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChildBot(Base):
    """Bot filho criado por alguém no bot mãe."""

    __tablename__ = "child_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)  # telegram_user_id do dono
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), default="")
    token_enc: Mapped[str] = mapped_column(Text)  # token criptografado
    status: Mapped[str] = mapped_column(String(16), default="active")  # active, stopped, invalid
    last_error: Mapped[str | None] = mapped_column(String(300))
    log_chat_id: Mapped[int | None] = mapped_column(BigInteger)  # canal/grupo de registros
    ban_network: Mapped[bool | None] = mapped_column(Boolean, default=True)  # ban em todos os grupos
    spam_names: Mapped[str | None] = mapped_column(Text, default="")  # anti-spam: um @ por linha
    spam_delay: Mapped[int | None] = mapped_column(Integer, default=60)  # segundos até apagar
    spam_deleted: Mapped[int | None] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChildAdmin(Base):
    """Pessoa que o dono liberou para mexer no painel do bot filho."""

    __tablename__ = "child_admins"
    __table_args__ = (UniqueConstraint("child_id", "telegram_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChildChat(Base):
    """Grupo ou canal onde um bot filho está."""

    __tablename__ = "child_chats"
    __table_args__ = (UniqueConstraint("child_id", "chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(256), default="")
    username: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16))  # group, supergroup, channel
    bot_status: Mapped[str] = mapped_column(String(16), default="member")  # administrator, member, left, kicked
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    can_restrict: Mapped[bool] = mapped_column(Boolean, default=False)
    can_post: Mapped[bool] = mapped_column(Boolean, default=False)
    can_pin: Mapped[bool] = mapped_column(Boolean, default=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    partner_of: Mapped[int | None] = mapped_column(Integer)  # grupo do PARCEIRO (id da parceria): não é seu
    # funções ligadas neste grupo (vazio = ligado)
    f_post: Mapped[bool | None] = mapped_column(Boolean, default=True)  # publicar (acervo, mensagens repetidas)
    f_words: Mapped[bool | None] = mapped_column(Boolean, default=True)
    f_spam: Mapped[bool | None] = mapped_column(Boolean, default=True)
    f_ban: Mapped[bool | None] = mapped_column(Boolean, default=True)
    f_join: Mapped[bool | None] = mapped_column(Boolean, default=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Setting(Base):
    """Configurações gerais da plataforma (chave → valor)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProcessedUpdate(Base):
    """Cada update já tratado (por bot), para descartar repetições do Telegram."""

    __tablename__ = "processed_updates"

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------- acervo ----------

class Acervo(Base):
    """Configuração do acervo de um bot filho (um por bot)."""

    __tablename__ = "acervos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), unique=True)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)  # canal de origem
    max_items: Mapped[int] = mapped_column(Integer, default=100)  # 0 = sem limite
    full_mode: Mapped[str] = mapped_column(String(8), default="rotate")  # rotate | lock
    order_mode: Mapped[str] = mapped_column(String(8), default="random")  # random | seq
    at_end: Mapped[str] = mapped_column(String(8), default="restart")  # restart | stop (sequencial)
    cursor: Mapped[int] = mapped_column(Integer, default=0)  # último seq postado (sequencial)
    last_item_id: Mapped[int | None] = mapped_column(Integer)
    caption_mode: Mapped[str] = mapped_column(String(8), default="original")  # original | fixed | random | none
    fixed_text: Mapped[str] = mapped_column(Text, default="")
    random_texts: Mapped[str] = mapped_column(Text, default="")  # separados por linha com ---
    footer_text: Mapped[str] = mapped_column(Text, default="")  # texto universal, sempre no fim
    buttons: Mapped[str] = mapped_column(Text, default="")  # formato "Texto - link", " | " na mesma linha
    caption_above: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    lock_warned: Mapped[bool] = mapped_column(Boolean, default=False)
    seq_counter: Mapped[int] = mapped_column(Integer, default=0)
    split_albums: Mapped[bool | None] = mapped_column(Boolean, default=False)  # álbum vira mídias soltas


class AcervoItem(Base):
    """Um post do acervo: uma foto, um vídeo ou um álbum."""

    __tablename__ = "acervo_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # ordem de chegada
    media_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer)
    caption_html: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[str] = mapped_column(Text, default="[]")  # JSON [{"t": "photo"|"video", "f": file_id}]
    state: Mapped[str] = mapped_column(String(10), default="active")  # active | archived | removed
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- programações (acervo e mensagens repetidas) ----------

class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # acervo | msg
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    start_time: Mapped[str] = mapped_column(String(5), default="00:00")  # cronograma (alinhamento)
    trigger: Mapped[str] = mapped_column(String(8), default="time")  # time | msgs
    interval_min: Mapped[int] = mapped_column(Integer, default=60)
    every_msgs: Mapped[int] = mapped_column(Integer, default=50)
    weekdays: Mapped[int] = mapped_column(Integer, default=127)  # bit 0 = segunda
    window_start: Mapped[int] = mapped_column(Integer, default=0)  # minutos do dia
    window_end: Mapped[int] = mapped_column(Integer, default=1440)
    start_date: Mapped[str | None] = mapped_column(String(10))  # AAAA-MM-DD
    end_date: Mapped[str | None] = mapped_column(String(10))
    max_per_day: Mapped[int] = mapped_column(Integer, default=0)  # 0 = sem limite
    posts_day: Mapped[str | None] = mapped_column(String(10))
    posts_today: Mapped[int] = mapped_column(Integer, default=0)
    pin: Mapped[int] = mapped_column(Integer, default=0)  # 0 não · 1 fixa sem notificar · 2 fixa notificando
    delete_prev: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delete_min: Mapped[int] = mapped_column(Integer, default=0)
    # conteúdo (só mensagens repetidas)
    text_html: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[str] = mapped_column(Text, default="[]")
    buttons: Mapped[str] = mapped_column(Text, default="")
    caption_above: Mapped[bool] = mapped_column(Boolean, default=False)  # mídia abaixo do texto
    name: Mapped[str | None] = mapped_column(String(60))
    partnership_id: Mapped[int | None] = mapped_column(Integer, index=True)  # divulgação de uma parceria
    content_order: Mapped[str | None] = mapped_column(String(8), default="seq")  # seq | random (vários conteúdos)
    content_cursor: Mapped[int | None] = mapped_column(Integer, default=0)
    content_last: Mapped[int | None] = mapped_column(Integer)
    text_variants: Mapped[str | None] = mapped_column(Text, default="[]")  # JSON: outras versões do texto
    text_cursor: Mapped[int | None] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScheduleItem(Base):
    """Um dos vários conteúdos de uma mensagem repetida (texto + mídia), usados em rodízio."""

    __tablename__ = "schedule_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    text_html: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[str] = mapped_column(Text, default="[]")
    media_group_id: Mapped[str | None] = mapped_column(String(64))
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScheduleDest(Base):
    __tablename__ = "schedule_dests"
    __table_args__ = (UniqueConstraint("schedule_id", "chat_row_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"), index=True)
    chat_row_id: Mapped[int] = mapped_column(ForeignKey("child_chats.id", ondelete="CASCADE"))
    msg_counter: Mapped[int] = mapped_column(Integer, default=0)
    last_message_ids: Mapped[str] = mapped_column(String(200), default="")


class PendingDelete(Base):
    """Mensagem que o bot vai apagar mais tarde (exclusão automática, avisos)."""

    __tablename__ = "pending_deletes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_ids: Mapped[str] = mapped_column(String(200))
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# ---------- palavras proibidas ----------

class WordFilter(Base):
    __tablename__ = "word_filters"
    __table_args__ = (UniqueConstraint("child_id", "chat_row_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    chat_row_id: Mapped[int] = mapped_column(ForeignKey("child_chats.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(8), default="off")  # off | warn | kick | mute | ban
    delete: Mapped[bool] = mapped_column(Boolean, default=True)
    duration_min: Mapped[int] = mapped_column(Integer, default=0)  # silenciar/ban: 0 = para sempre
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    warn_then: Mapped[str] = mapped_column(String(8), default="mute")  # mute | kick | ban
    match: Mapped[str] = mapped_column(String(8), default="contains")  # contains | exact
    words: Mapped[str] = mapped_column(Text, default="")  # uma por linha


class Partnership(Base):
    """Parceria de divulgação entre dois bots filhos: um posta nos grupos do outro, dentro do acordo."""

    __tablename__ = "partnerships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    a_child_id: Mapped[int] = mapped_column(Integer, index=True)  # quem convidou
    b_child_id: Mapped[int | None] = mapped_column(Integer, index=True)  # quem aceitou
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | invited | active | paused | ended
    min_interval: Mapped[int] = mapped_column(Integer, default=60)  # minutos entre posts, mínimo combinado
    max_day: Mapped[int] = mapped_column(Integer, default=10)  # posts por dia em cada grupo
    window_start: Mapped[int] = mapped_column(Integer, default=0)
    window_end: Mapped[int] = mapped_column(Integer, default=1440)
    allow_media: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_buttons: Mapped[bool] = mapped_column(Boolean, default=True)
    end_date: Mapped[str | None] = mapped_column(String(10))
    paused_by: Mapped[int | None] = mapped_column(Integer)  # child_id de quem pausou
    ended_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)


class PartnerChat(Base):
    """Grupo/canal que um lado liberou para o parceiro postar."""

    __tablename__ = "partner_chats"
    __table_args__ = (UniqueConstraint("partnership_id", "chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partnership_id: Mapped[int] = mapped_column(ForeignKey("partnerships.id", ondelete="CASCADE"), index=True)
    owner_child_id: Mapped[int] = mapped_column(Integer)  # dono do grupo
    chat_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(256), default="")
    type: Mapped[str] = mapped_column(String(16), default="supergroup")


class ChildBan(Base):
    """Quem foi banido em todos os grupos de um bot filho."""

    __tablename__ = "child_bans"
    __table_args__ = (UniqueConstraint("child_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(256), default="")
    username: Mapped[str | None] = mapped_column(String(64))
    source_title: Mapped[str] = mapped_column(String(256), default="")
    actor: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class JoinFilter(Base):
    """Pedidos de entrada de um grupo/canal: aprova ou recusa olhando o nome e a bio."""

    __tablename__ = "join_filters"
    __table_args__ = (UniqueConstraint("child_id", "chat_row_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_bots.id", ondelete="CASCADE"), index=True)
    chat_row_id: Mapped[int] = mapped_column(ForeignKey("child_chats.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    block_words: Mapped[str] = mapped_column(Text, default="")  # nome ou bio com estas → recusa
    allow_words: Mapped[str] = mapped_column(Text, default="")  # se tiver lista, só aprova quem tem uma delas
    on_block: Mapped[str] = mapped_column(String(8), default="decline")  # decline | hold
    on_clean: Mapped[str] = mapped_column(String(8), default="approve")  # approve | hold
    need_username: Mapped[bool | None] = mapped_column(Boolean, default=False)  # recusa quem não tem @


class WarnCount(Base):
    __tablename__ = "warnings"
    __table_args__ = (UniqueConstraint("child_id", "chat_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(Integer)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def _add_missing_columns(conn) -> None:
    """Atualiza tabelas de versões antigas: cria as colunas novas que faltam (sempre opcionais)."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(conn.dialect)}")


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        self.engine = create_async_engine(url)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    async def prune_updates(self, hours: int = 48) -> int:
        cutoff = utcnow() - timedelta(hours=hours)
        async with self.session() as s:
            result = await s.execute(delete(ProcessedUpdate).where(ProcessedUpdate.received_at < cutoff))
            await s.commit()
            return result.rowcount or 0

    async def close(self) -> None:
        await self.engine.dispose()
