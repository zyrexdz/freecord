import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Bot, Backup, MemberToken, PullTask, AuditLog, GuildConfig
from web.routes.auth import require_login
from services.bot_manager import BotManager
from core.config import detect_network_addresses

logger = logging.getLogger("freecord.web.dashboard")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_overview(
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):

    from core.access_control import get_user_accessible_bot_ids
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    if not bot_ids:
        total_bots = 0
        total_verified = 0
        total_backups = 0
        total_migrations = 0
        total_restored = 0
        total_vpns_blocked = 0
        online_bots_count = 0
        total_guilds = 0
        recent_logs = []
    else:
        bot_count_res = await db.execute(select(func.count(Bot.id)).where(Bot.id.in_(bot_ids)))
        total_bots = bot_count_res.scalar() or 0

        token_count_res = await db.execute(
            select(func.count(func.distinct(MemberToken.user_id))).where(MemberToken.bot_id.in_(bot_ids))
        )
        total_verified = token_count_res.scalar() or 0

        backup_count_res = await db.execute(select(func.count(Backup.id)).where(Backup.bot_id.in_(bot_ids)))
        total_backups = backup_count_res.scalar() or 0

        pull_count_res = await db.execute(select(func.count(PullTask.id)).where(PullTask.bot_id.in_(bot_ids)))
        total_migrations = pull_count_res.scalar() or 0

        restored_res = await db.execute(
            select(func.sum(PullTask.success_count)).where(PullTask.bot_id.in_(bot_ids))
        )
        raw_restored = restored_res.scalar() or 0
        total_restored = min(raw_restored, total_verified)

        vpn_count_res = await db.execute(
            select(func.count(MemberToken.id)).where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.is_vpn == True))
        )
        total_vpns_blocked = vpn_count_res.scalar() or 0

        active_bot_instances = BotManager.get_all_active_bots()
        online_bots_count = sum(1 for bid, b in active_bot_instances.items() if bid in bot_ids and b.is_ready())
        total_guilds = sum(len(b.guilds) for bid, b in active_bot_instances.items() if bid in bot_ids and b.is_ready())

        cfg_stmt = select(GuildConfig.guild_id).where(GuildConfig.bot_id.in_(bot_ids))
        user_guild_ids = list((await db.execute(cfg_stmt)).scalars().all())

        if user_guild_ids:
            logs_res = await db.execute(
                select(AuditLog)
                .where((AuditLog.guild_id.in_(user_guild_ids)) | (AuditLog.guild_id == None))
                .order_by(AuditLog.created_at.desc())
                .limit(8)
            )
            recent_logs = logs_res.scalars().all()
        else:
            recent_logs = []

    net_info = detect_network_addresses()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": current_user,
            "total_bots": total_bots,
            "online_bots_count": online_bots_count,
            "total_verified": total_verified,
            "total_restored": total_restored,
            "total_backups": total_backups,
            "total_migrations": total_migrations,
            "total_vpns_blocked": total_vpns_blocked,
            "total_guilds": total_guilds,
            "recent_logs": recent_logs,
            "net_info": net_info,
        },
    )
