import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Bot, GuildConfig
from web.routes.auth import require_login
from services.bot_manager import BotManager
from core.config import detect_network_addresses

logger = logging.getLogger("freecord.web.servers")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


async def get_guild_text_channels(guild):
    channels = []
    for c in getattr(guild, "text_channels", []):
        channels.append({"id": str(c.id), "name": c.name})
    if not channels and hasattr(guild, "channels"):
        for c in guild.channels:
            if hasattr(c, "send") and not str(c.type) in ("voice", "stage_voice", "category", "2", "4", "13"):
                channels.append({"id": str(c.id), "name": c.name})
    if not channels and hasattr(guild, "fetch_channels"):
        try:
            fetched = await guild.fetch_channels()
            for c in fetched:
                if hasattr(c, "send") and not str(c.type) in ("voice", "stage_voice", "category", "2", "4", "13"):
                    channels.append({"id": str(c.id), "name": c.name})
        except Exception:
            pass
    return channels


@router.get("/servers", response_class=HTMLResponse)
async def all_servers_view(
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import get_user_accessible_bot_ids, can_user_access_bot, get_user_bot_permissions
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    if not bot_ids:
        bots = []
    else:
        stmt = select(Bot).where(Bot.id.in_(bot_ids))
        res = await db.execute(stmt)
        bots = list(res.scalars().all())

    all_servers = []
    net_info = detect_network_addresses()

    for bot in bots:
        client = BotManager.get_bot(bot.id)
        if client and client.is_ready():
            perms = await get_user_bot_permissions(db, current_user, bot.id)
            allowed_guilds = perms.get("allowed_guilds", ["ALL"])

            for guild in client.guilds:
                if "ALL" not in allowed_guilds and str(guild.id) not in allowed_guilds:
                    continue

                cfg_stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == str(guild.id)) & (GuildConfig.bot_id == bot.id)
                )
                cfg_res = await db.execute(cfg_stmt)
                cfg = cfg_res.scalars().first()

                roles = [{"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}"} for r in guild.roles if not r.managed and not r.is_default()]
                text_channels = await get_guild_text_channels(guild)
                verify_url = f"{net_info['recommended_base_url']}/verify/{bot.id}/{guild.id}"

                all_servers.append({
                    "bot_id": bot.id,
                    "bot_name": bot.name,
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon_url": str(guild.icon.url) if guild.icon else None,
                    "member_count": guild.member_count,
                    "channels_count": len(guild.channels),
                    "roles_count": len(guild.roles),
                    "roles": roles,
                    "text_channels": text_channels,
                    "config": cfg,
                    "verify_url": verify_url,
                })

    return templates.TemplateResponse(
        request=request,
        name="servers.html",
        context={
            "user": current_user,
            "bots": bots,
            "servers": all_servers,
            "net_info": net_info,
        },
    )


@router.get("/bots/{bot_id}/servers", response_class=HTMLResponse)
async def bot_servers_view(
    bot_id: int,
    request: Request,
    guild_id: Optional[str] = Query(None),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import can_user_access_bot, get_user_bot_permissions
    if not await can_user_access_bot(db, current_user, bot_id):
        return RedirectResponse(url="/servers?error=Unauthorized+access+to+bot", status_code=303)

    stmt = select(Bot).where(Bot.id == bot_id)
    res = await db.execute(stmt)
    bot = res.scalar_one_or_none()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    perms = await get_user_bot_permissions(db, current_user, bot_id)
    allowed_guilds = perms.get("allowed_guilds", ["ALL"])

    client = BotManager.get_bot(bot_id)
    guild_items = []
    net_info = detect_network_addresses()

    if client and client.is_ready():
        for guild in client.guilds:
            if "ALL" not in allowed_guilds and str(guild.id) not in allowed_guilds:
                continue
            cfg_stmt = select(GuildConfig).where(
                (GuildConfig.guild_id == str(guild.id)) & (GuildConfig.bot_id == bot_id)
            )
            cfg_res = await db.execute(cfg_stmt)
            cfg = cfg_res.scalars().first()

            roles = [{"id": str(r.id), "name": r.name, "color": f"#{r.color.value:06x}"} for r in guild.roles if not r.managed and not r.is_default()]
            text_channels = await get_guild_text_channels(guild)

            verify_url = f"{net_info['recommended_base_url']}/verify/{bot_id}/{guild.id}"

            guild_items.append({
                "id": str(guild.id),
                "name": guild.name,
                "icon_url": str(guild.icon.url) if guild.icon else None,
                "member_count": guild.member_count,
                "channels_count": len(guild.channels),
                "roles_count": len(guild.roles),
                "roles": roles,
                "text_channels": text_channels,
                "config": cfg,
                "verify_url": verify_url,
            })

    return templates.TemplateResponse(
        request=request,
        name="bot_servers.html",
        context={
            "user": current_user,
            "bot": bot,
            "guild_items": guild_items,
            "selected_guild_id": guild_id,
            "is_live": client is not None and client.is_ready(),
            "net_info": net_info,
        },
    )


@router.post("/bots/{bot_id}/servers/{guild_id}/save")
async def save_guild_config(
    bot_id: int,
    guild_id: str,
    verified_role_id: Optional[str] = Form(None),
    unverified_role_id: Optional[str] = Form(None),
    log_channel_id: Optional[str] = Form(None),
    webhook_url: Optional[str] = Form(None),
    firewall_enabled: bool = Form(False),
    anti_vpn_enabled: bool = Form(False),
    block_datacenter: bool = Form(False),
    block_cellular: bool = Form(False),
    min_account_age_days: int = Form(0),
    captcha_enabled: bool = Form(False),
    captcha_provider: str = Form("turnstile"),
    auto_pull_enabled: bool = Form(False),
    auto_pull_backup_guild_id: Optional[str] = Form(None),
    custom_branding_title: Optional[str] = Form("Verification Portal"),
    custom_branding_desc: Optional[str] = Form("Secure Discord verification"),
    theme_color: Optional[str] = Form("#5865F2"),
    bg_image_url: Optional[str] = Form(None),
    music_url: Optional[str] = Form(None),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(GuildConfig).where(
        (GuildConfig.guild_id == guild_id) & (GuildConfig.bot_id == bot_id)
    )
    res = await db.execute(stmt)
    cfg = res.scalars().first()

    if not cfg:
        cfg = GuildConfig(
            guild_id=guild_id,
            bot_id=bot_id,
        )
        db.add(cfg)

    client = BotManager.get_bot(bot_id)
    if client:
        g = client.get_guild(int(guild_id))
        if g:
            cfg.guild_name = g.name
            if g.icon:
                cfg.guild_icon = str(g.icon.url)

    cfg.verified_role_id = verified_role_id if verified_role_id else None
    cfg.unverified_role_id = unverified_role_id if unverified_role_id else None
    cfg.log_channel_id = log_channel_id if log_channel_id else None
    cfg.webhook_url = webhook_url.strip() if webhook_url and webhook_url.strip() else None
    cfg.firewall_enabled = firewall_enabled
    cfg.anti_vpn_enabled = anti_vpn_enabled
    cfg.block_datacenter = block_datacenter
    cfg.block_cellular = block_cellular
    cfg.min_account_age_days = max(0, min_account_age_days)
    cfg.captcha_enabled = captcha_enabled
    cfg.captcha_provider = captcha_provider
    cfg.auto_pull_enabled = auto_pull_enabled
    cfg.auto_pull_backup_guild_id = auto_pull_backup_guild_id if auto_pull_backup_guild_id else None
    cfg.custom_branding_title = custom_branding_title
    cfg.custom_branding_desc = custom_branding_desc
    cfg.theme_color = theme_color
    cfg.bg_image_url = bg_image_url.strip() if bg_image_url and bg_image_url.strip() else None
    cfg.music_url = music_url.strip() if music_url and music_url.strip() else None

    await db.commit()

    return RedirectResponse(
        url=f"/bots/{bot_id}/servers?guild_id={guild_id}&success=Server+settings+saved#server-{guild_id}",
        status_code=303,
    )


@router.post("/bots/{bot_id}/servers/{guild_id}/create-verified-role")
async def create_verified_role(
    bot_id: int,
    guild_id: str,
    current_user: User = Depends(require_login),
):
    client = BotManager.get_bot(bot_id)
    if not client or not client.is_ready():
        return RedirectResponse(
            url=f"/bots/{bot_id}/servers?guild_id={guild_id}&error=Bot+must+be+online+to+create+roles",
            status_code=303,
        )

    guild = client.get_guild(int(guild_id))
    if not guild:
        return RedirectResponse(
            url=f"/bots/{bot_id}/servers?guild_id={guild_id}&error=Server+not+found",
            status_code=303,
        )

    role = await BotManager.ensure_verified_role(bot_id, guild)
    if not role:
        return RedirectResponse(
            url=f"/bots/{bot_id}/servers?guild_id={guild_id}&error=Could+not+create+role.+Check+bot+permissions",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/bots/{bot_id}/servers?guild_id={guild_id}&success=Verified+role+created+and+assigned#server-{guild_id}",
        status_code=303,
    )


@router.post("/bots/{bot_id}/servers/{guild_id}/send-verify-message")
@router.post("/servers/{bot_id}/{guild_id}/send-verify-message")
async def send_verify_message(
    bot_id: int,
    guild_id: str,
    channel_id: str = Form(...),
    title: Optional[str] = Form("Server Verification"),
    description: Optional[str] = Form("Click the button below to verify and get your role."),
    button_label: Optional[str] = Form("Verify"),
    color_hex: Optional[str] = Form("#5865F2"),
    redirect_to: Optional[str] = Form(None),
    current_user: User = Depends(require_login),
):
    success, msg = await BotManager.send_verify_embed_message(
        bot_db_id=bot_id,
        guild_id=guild_id,
        channel_id=channel_id,
        title=title,
        description=description,
        button_label=button_label,
        color_hex=color_hex,
    )
    target_url = redirect_to or f"/bots/{bot_id}/servers?guild_id={guild_id}"
    param = "success" if success else "error"
    clean_msg = msg.replace(" ", "+")
    return RedirectResponse(
        url=f"{target_url}&{param}={clean_msg}#server-{guild_id}" if "?" in target_url else f"{target_url}?{param}={clean_msg}#server-{guild_id}",
        status_code=303,
    )
