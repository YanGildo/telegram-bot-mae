"""Leitura e escrita no banco."""

from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db import Account, ChildAdmin, ChildBot, ChildChat, Database, Setting

MAX_BOTS_PER_USER = 10  # para quem não é dono da plataforma


# ---------- configurações gerais ----------

async def get_setting(db: Database, key: str, default: str = "") -> str:
    async with db.session() as s:
        row = await s.get(Setting, key)
        return row.value if row is not None else default


async def set_setting(db: Database, key: str, value: str) -> None:
    async with db.session() as s:
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        await s.commit()


async def is_open(db: Database) -> bool:
    """Plataforma aberta para todos? (padrão: fechada, só donos e liberados)"""
    return (await get_setting(db, "open_to_all", "0")) == "1"


# ---------- contas ----------

async def touch_account(db: Database, user: TgUser, referrer: str | None = None) -> Account:
    async with db.session() as s:
        acc = await s.scalar(select(Account).where(Account.telegram_user_id == user.id))
        if acc is None:
            acc = Account(telegram_user_id=user.id)
            s.add(acc)
        if referrer and not acc.referrer:
            acc.referrer = referrer  # guarda só o primeiro link pelo qual a pessoa chegou
        acc.first_name, acc.username = user.first_name, user.username
        await s.commit()
        return acc


async def is_platform_admin(db: Database, settings: Settings, user_id: int | None) -> bool:
    """Dono (ADMIN_IDS do .env) ou admin que o dono colocou pelo bot."""
    if user_id is None:
        return False
    if settings.is_owner(user_id):
        return True
    async with db.session() as s:
        acc = await s.scalar(select(Account).where(Account.telegram_user_id == user_id))
        return bool(acc and acc.platform_admin)


async def platform_admins(db: Database) -> list[Account]:
    async with db.session() as s:
        return list(await s.scalars(select(Account).where(Account.platform_admin.is_(True)).order_by(Account.id)))


async def set_platform_admin(db: Database, user_id: int, on: bool) -> None:
    async with db.session() as s:
        acc = await s.scalar(select(Account).where(Account.telegram_user_id == user_id))
        if acc is None:
            acc = Account(telegram_user_id=user_id)
            s.add(acc)
        acc.platform_admin = on
        await s.commit()


async def can_create(db: Database, settings: Settings, user_id: int) -> bool:
    if await is_platform_admin(db, settings, user_id) or await is_open(db):
        return True
    async with db.session() as s:
        acc = await s.scalar(select(Account).where(Account.telegram_user_id == user_id))
        return bool(acc and acc.allowed)


async def allowed_accounts(db: Database) -> list[Account]:
    async with db.session() as s:
        return list(await s.scalars(select(Account).where(Account.allowed.is_(True)).order_by(Account.id)))


async def set_allowed(db: Database, user_id: int, allowed: bool) -> None:
    async with db.session() as s:
        acc = await s.scalar(select(Account).where(Account.telegram_user_id == user_id))
        if acc is None:
            acc = Account(telegram_user_id=user_id)
            s.add(acc)
        acc.allowed = allowed
        await s.commit()


# ---------- bots filhos ----------

async def bots_of(db: Database, owner_id: int) -> list[ChildBot]:
    async with db.session() as s:
        return list(await s.scalars(select(ChildBot).where(ChildBot.owner_id == owner_id).order_by(ChildBot.id)))


async def all_bots(db: Database) -> list[ChildBot]:
    async with db.session() as s:
        return list(await s.scalars(select(ChildBot).order_by(ChildBot.id)))


async def get_bot(db: Database, child_id: int) -> ChildBot | None:
    async with db.session() as s:
        return await s.get(ChildBot, child_id)


async def bot_by_telegram_id(db: Database, bot_id: int) -> ChildBot | None:
    async with db.session() as s:
        return await s.scalar(select(ChildBot).where(ChildBot.telegram_bot_id == bot_id))


async def chat_count(s: AsyncSession, child_id: int) -> int:
    return await s.scalar(select(func.count(ChildChat.id)).where(
        ChildChat.child_id == child_id, ChildChat.bot_status.in_(("administrator", "member")))) or 0


# ---------- admins do bot filho ----------

async def is_child_admin(db: Database, child: ChildBot, user_id: int) -> bool:
    if user_id == child.owner_id:
        return True
    async with db.session() as s:
        return await s.scalar(select(ChildAdmin.id).where(
            ChildAdmin.child_id == child.id, ChildAdmin.telegram_user_id == user_id)) is not None


async def child_admins(db: Database, child_id: int) -> list[ChildAdmin]:
    async with db.session() as s:
        return list(await s.scalars(select(ChildAdmin).where(ChildAdmin.child_id == child_id).order_by(ChildAdmin.id)))


async def child_chats(db: Database, child_id: int, only_present: bool = True) -> list[ChildChat]:
    async with db.session() as s:
        q = select(ChildChat).where(ChildChat.child_id == child_id)
        if only_present:
            q = q.where(ChildChat.bot_status.in_(("administrator", "member", "creator")))
        return list(await s.scalars(q.order_by(ChildChat.type, ChildChat.title)))
