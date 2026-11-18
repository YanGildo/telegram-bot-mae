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
