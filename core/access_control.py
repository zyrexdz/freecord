import json
from typing import List, Set, Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import User, Bot, BotCollaborator


async def get_user_accessible_bot_ids(db: AsyncSession, user: User) -> List[int]:
    if user.role == "admin" or user.id == 1:
        res = await db.execute(select(Bot.id))
        return list(res.scalars().all())

    owned_res = await db.execute(select(Bot.id).where(Bot.owner_id == user.id))
    owned_ids = set(owned_res.scalars().all())

    collab_res = await db.execute(
        select(BotCollaborator.bot_id).where(
            (BotCollaborator.user_id == user.id) | (BotCollaborator.username == user.username)
        )
    )
    collab_ids = set(collab_res.scalars().all())

    return list(owned_ids.union(collab_ids))


async def can_user_access_bot(db: AsyncSession, user: User, bot_id: int) -> bool:
    if user.role == "admin" or user.id == 1:
        return True

    res = await db.execute(
        select(Bot).where((Bot.id == bot_id) & (Bot.owner_id == user.id))
    )
    if res.scalars().first():
        return True

    collab_res = await db.execute(
        select(BotCollaborator).where(
            (BotCollaborator.bot_id == bot_id) &
            ((BotCollaborator.user_id == user.id) | (BotCollaborator.username == user.username))
        )
    )
    return collab_res.scalars().first() is not None


async def get_user_bot_permissions(db: AsyncSession, user: User, bot_id: int) -> Dict[str, Any]:
    if user.role == "admin" or user.id == 1:
        return {
            "is_owner": True,
            "can_manage_backups": True,
            "can_start_migrations": True,
            "can_view_member_details": True,
            "can_manage_blacklist": True,
            "can_manage_settings": True,
            "can_export_tokens": True,
            "allowed_guilds": ["ALL"],
        }

    res = await db.execute(
        select(Bot).where((Bot.id == bot_id) & (Bot.owner_id == user.id))
    )
    if res.scalars().first():
        return {
            "is_owner": True,
            "can_manage_backups": True,
            "can_start_migrations": True,
            "can_view_member_details": True,
            "can_manage_blacklist": True,
            "can_manage_settings": True,
            "can_export_tokens": True,
            "allowed_guilds": ["ALL"],
        }

    collab_res = await db.execute(
        select(BotCollaborator).where(
            (BotCollaborator.bot_id == bot_id) &
            ((BotCollaborator.user_id == user.id) | (BotCollaborator.username == user.username))
        )
    )
    collab = collab_res.scalars().first()
    if collab:
        try:
            guilds = json.loads(collab.allowed_guilds_json) if collab.allowed_guilds_json else ["ALL"]
        except Exception:
            guilds = ["ALL"]
        return {
            "is_owner": False,
            "can_manage_backups": collab.can_manage_backups,
            "can_start_migrations": collab.can_start_migrations,
            "can_view_member_details": collab.can_view_member_details,
            "can_manage_blacklist": collab.can_manage_blacklist,
            "can_manage_settings": collab.can_manage_settings,
            "can_export_tokens": collab.can_export_tokens,
            "allowed_guilds": guilds,
        }

    return {
        "is_owner": False,
        "can_manage_backups": False,
        "can_start_migrations": False,
        "can_view_member_details": False,
        "can_manage_blacklist": False,
        "can_manage_settings": False,
        "can_export_tokens": False,
        "allowed_guilds": [],
    }
