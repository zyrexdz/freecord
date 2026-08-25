import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Blacklist, PlatformSetting
from web.routes.auth import require_admin, require_login
from core.config import get_settings, detect_network_addresses
from core.security import get_password_hash

logger = logging.getLogger("freecord.web.settings")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()

    users_res = await db.execute(select(User).order_by(User.id.asc()))
    staff_users = users_res.scalars().all()

    bl_res = await db.execute(select(Blacklist).order_by(Blacklist.created_at.desc()))
    blacklists = bl_res.scalars().all()

    from core.access_control import get_user_accessible_bot_ids
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    from services.bot_manager import BotManager
    available_guilds = []
    active_instances = BotManager.get_all_active_bots()
    for bot_id, client in active_instances.items():
        if bot_id in bot_ids and client and client.is_ready():
            bot_name = client.user.name if client.user else f"Bot {bot_id}"
            for g in client.guilds:
                available_guilds.append({
                    "id": str(g.id),
                    "name": g.name,
                    "bot_name": bot_name,
                })

    discord_oauth_stmt = select(PlatformSetting).where(PlatformSetting.key == "discord_oauth_config")
    oauth_res = await db.execute(discord_oauth_stmt)
    oauth_setting = oauth_res.scalars().first()
    import json
    discord_oauth_config = {}
    if oauth_setting:
        try:
            discord_oauth_config = json.loads(oauth_setting.value_json)
        except Exception:
            pass
    if not discord_oauth_config and settings.DISCORD_CLIENT_ID:
        discord_oauth_config = {
            "enabled": settings.DISCORD_OAUTH_ENABLED,
            "client_id": settings.DISCORD_CLIENT_ID,
            "client_secret": settings.DISCORD_CLIENT_SECRET or "",
        }

    net_info = detect_network_addresses()

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "user": current_user,
            "settings": settings,
            "staff_users": staff_users,
            "blacklists": blacklists,
            "available_guilds": available_guilds,
            "discord_oauth_config": discord_oauth_config,
            "net_info": net_info,
        },
    )


@router.get("/api/v1/network/status")
async def network_status_endpoint():
    return JSONResponse(detect_network_addresses())


@router.post("/settings/discord-oauth/save")
async def save_discord_oauth_settings(
    enabled: Optional[bool] = Form(False),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role != "admin" and current_user.id != 1:
        raise HTTPException(status_code=403, detail="Admin role required")

    import json
    stmt = select(PlatformSetting).where(PlatformSetting.key == "discord_oauth_config")
    setting = (await db.execute(stmt)).scalars().first()

    existing_config = {}
    if setting:
        try:
            existing_config = json.loads(setting.value_json)
        except Exception:
            pass

    clean_cid = client_id.strip() if client_id else ""
    clean_secret = client_secret.strip() if client_secret else existing_config.get("client_secret", "")

    config_data = {
        "enabled": bool(enabled),
        "client_id": clean_cid,
        "client_secret": clean_secret,
    }

    if not setting:
        setting = PlatformSetting(
            key="discord_oauth_config",
            value_json=json.dumps(config_data),
        )
        db.add(setting)
    else:
        setting.value_json = json.dumps(config_data)

    await db.commit()

    s = get_settings()
    s.DISCORD_OAUTH_ENABLED = bool(enabled)
    s.DISCORD_CLIENT_ID = clean_cid if clean_cid else None
    s.DISCORD_CLIENT_SECRET = clean_secret if clean_secret else None

    return RedirectResponse(url="/settings?success=Discord+login+settings+saved+successfully", status_code=303)


@router.post("/settings/api-keys/save")
async def save_api_keys(
    turnstile_site_key: Optional[str] = Form(None),
    turnstile_secret_key: Optional[str] = Form(None),
    hcaptcha_site_key: Optional[str] = Form(None),
    hcaptcha_secret_key: Optional[str] = Form(None),
    ipqualityscore_api_key: Optional[str] = Form(None),
    current_user: User = Depends(require_admin),
):
    if current_user.role != "admin" and current_user.id != 1:
        raise HTTPException(status_code=403, detail="Admin role required")

    settings = get_settings()
    settings.TURNSTILE_SITE_KEY = turnstile_site_key.strip() if turnstile_site_key else None
    settings.TURNSTILE_SECRET_KEY = turnstile_secret_key.strip() if turnstile_secret_key else None
    settings.HCAPTCHA_SITE_KEY = hcaptcha_site_key.strip() if hcaptcha_site_key else None
    settings.HCAPTCHA_SECRET_KEY = hcaptcha_secret_key.strip() if hcaptcha_secret_key else None
    settings.IPQUALITYSCORE_API_KEY = ipqualityscore_api_key.strip() if ipqualityscore_api_key else None

    return RedirectResponse(url="/settings?success=API+keys+updated+successfully", status_code=303)


@router.post("/settings/blacklist/add")
async def add_blacklist_entry(
    type: str = Form("USER_ID"),
    value: str = Form(...),
    guild_id: Optional[str] = Form(None),
    reason: Optional[str] = Form("Manual Dashboard Blacklist"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target_guild = None if not guild_id or guild_id.strip() == "ALL" else guild_id.strip()
    bl = Blacklist(
        type=type.strip(),
        value=value.strip(),
        guild_id=target_guild,
        reason=reason.strip() if reason else "Manual Dashboard Blacklist",
        added_by=current_user.username,
    )
    db.add(bl)
    await db.commit()
    return RedirectResponse(url="/settings?success=Blacklist+entry+added", status_code=303)


@router.post("/settings/password/change")
async def change_password(
    current_password: Optional[str] = Form(None),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.security import verify_password, hash_password

    if new_password != confirm_password:
        return RedirectResponse(url="/settings?error=New+passwords+do+not+match", status_code=303)

    if len(new_password) < 6:
        return RedirectResponse(url="/settings?error=Password+must+be+at+least+6+characters", status_code=303)

    is_discord_oauth = current_user.password_hash and current_user.password_hash.startswith("DISCORD_OAUTH_")
    if not is_discord_oauth:
        if not current_password or not verify_password(current_password, current_user.password_hash):
            return RedirectResponse(url="/settings?error=Current+password+is+incorrect", status_code=303)

    current_user.password_hash = hash_password(new_password)
    db.add(current_user)
    await db.commit()
    return RedirectResponse(url="/settings?success=Password+updated+successfully", status_code=303)



@router.api_route("/settings/blacklist/{bl_id}/delete", methods=["GET", "POST"])
async def delete_blacklist_entry(
    bl_id: int,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from database.models import MemberToken
    stmt = select(Blacklist).where(Blacklist.id == bl_id)
    res = await db.execute(stmt)
    entry = res.scalars().first()
    if entry:
        if entry.type == "USER_ID":
            mem_stmt = select(MemberToken).where(MemberToken.user_id == entry.value)
            for m in (await db.execute(mem_stmt)).scalars().all():
                m.is_blacklisted = False
        elif entry.type == "IP":
            mem_stmt = select(MemberToken).where(MemberToken.ip_address == entry.value)
            for m in (await db.execute(mem_stmt)).scalars().all():
                m.is_blacklisted = False
        await db.delete(entry)
        await db.commit()
    return RedirectResponse(url="/settings?success=Blacklist+entry+deleted", status_code=303)
