import json
import logging
from typing import Dict, Any, List
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, MemberToken, AuditLog
from web.routes.auth import require_login

logger = logging.getLogger("freecord.web.analytics")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_dashboard(
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    total_members = 0
    vpn_count = 0
    cellular_count = 0
    residential_count = 0
    country_labels: List[str] = []
    country_values: List[int] = []
    isp_labels: List[str] = []
    isp_values: List[int] = []
    alt_clusters: List[Dict[str, Any]] = []
    audit_logs: List[Any] = []

    from core.access_control import get_user_accessible_bot_ids
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    if bot_ids:
        total_tokens_res = await db.execute(select(func.count(MemberToken.id)).where(MemberToken.bot_id.in_(bot_ids)))
        total_members = total_tokens_res.scalar() or 0

        vpn_count_res = await db.execute(select(func.count(MemberToken.id)).where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.is_vpn == True)))
        vpn_count = vpn_count_res.scalar() or 0

        cellular_count_res = await db.execute(select(func.count(MemberToken.id)).where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.is_cellular == True)))
        cellular_count = cellular_count_res.scalar() or 0

        residential_count = max(0, total_members - vpn_count)

        country_res = await db.execute(
            select(MemberToken.country, func.count(MemberToken.id))
            .where(MemberToken.bot_id.in_(bot_ids))
            .group_by(MemberToken.country)
            .order_by(func.count(MemberToken.id).desc())
            .limit(6)
        )
        country_data = country_res.all()
        country_labels = [c[0] or "Unknown" for c in country_data]
        country_values = [c[1] for c in country_data]

        isp_res = await db.execute(
            select(MemberToken.isp, func.count(MemberToken.id))
            .where(MemberToken.bot_id.in_(bot_ids))
            .group_by(MemberToken.isp)
            .order_by(func.count(MemberToken.id).desc())
            .limit(6)
        )
        isp_data = isp_res.all()
        isp_labels = [i[0] or "Unknown" for i in isp_data]
        isp_values = [i[1] for i in isp_data]

        alt_ips_res = await db.execute(
            select(MemberToken.ip_address, func.count(MemberToken.id))
            .where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.ip_address != None) & (MemberToken.ip_address != "127.0.0.1") & (MemberToken.ip_address != "Local Network"))
            .group_by(MemberToken.ip_address)
            .having(func.count(MemberToken.id) > 1)
            .order_by(func.count(MemberToken.id).desc())
            .limit(8)
        )
        alt_clusters = []
        for ip, count in alt_ips_res.all():
            toks = (await db.execute(select(MemberToken).where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.ip_address == ip)))).scalars().all()
            alt_clusters.append({
                "ip": ip,
                "count": count,
                "users": [t.username for t in toks],
            })

        from database.models import GuildConfig
        cfg_stmt = select(GuildConfig.guild_id).where(GuildConfig.bot_id.in_(bot_ids))
        user_guild_ids = list((await db.execute(cfg_stmt)).scalars().all())

        if user_guild_ids:
            audit_res = await db.execute(
                select(AuditLog)
                .where((AuditLog.guild_id.in_(user_guild_ids)) | (AuditLog.guild_id == None))
                .order_by(AuditLog.created_at.desc())
                .limit(15)
            )
            audit_logs = audit_res.scalars().all()
        else:
            audit_logs = []

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "user": current_user,
            "total_members": total_members,
            "vpn_count": vpn_count,
            "cellular_count": cellular_count,
            "residential_count": residential_count,
            "country_labels_json": json.dumps(country_labels),
            "country_values_json": json.dumps(country_values),
            "isp_labels_json": json.dumps(isp_labels),
            "isp_values_json": json.dumps(isp_values),
            "alt_clusters": alt_clusters,
            "audit_logs": audit_logs,
        },
    )
