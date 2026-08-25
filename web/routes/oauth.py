import base64
import json
import logging
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db, async_session_factory
from database.models import Bot, GuildConfig, MemberToken
from core.security import encrypt_secret, decrypt_secret
from core.config import detect_network_addresses, get_settings
from services.security_service import SecurityService
from services.bot_manager import BotManager
from services.webhook_service import WebhookService

logger = logging.getLogger("freecord.web.oauth")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def get_client_ip(request: Request, client_override: Optional[str] = None) -> str:
    if client_override and ":" not in client_override and len(client_override.split(".")) == 4:
        return client_override.strip()

    headers = request.headers
    candidates = []

    for h in ["cf-connecting-ipv4", "cf-connecting-ip", "x-real-ip", "true-client-ip", "x-client-ip"]:
        val = headers.get(h)
        if val:
            candidates.append(val.strip())

    x_forwarded_for = headers.get("x-forwarded-for")
    if x_forwarded_for:
        for part in x_forwarded_for.split(","):
            if part.strip():
                candidates.append(part.strip())

    if request.client and request.client.host:
        candidates.append(request.client.host.strip())

    for ip in candidates:
        if ip.startswith("::ffff:"):
            return ip.replace("::ffff:", "")
        if ":" not in ip and ip != "127.0.0.1" and not ip.startswith("192.168.") and not ip.startswith("10."):
            return ip

    for ip in candidates:
        if ":" not in ip:
            return ip

    net = detect_network_addresses()
    if net.get("public_ip") and ":" not in net["public_ip"]:
        return net["public_ip"]

    if candidates:
        return candidates[0]

    return "127.0.0.1"


@router.get("/verify/{bot_id}/{guild_id}", response_class=HTMLResponse)
async def verification_portal(
    bot_id: int,
    guild_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    bot_res = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = bot_res.scalar_one_or_none()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    cfg_res = await db.execute(
        select(GuildConfig).where(
            (GuildConfig.guild_id == guild_id) & (GuildConfig.bot_id == bot_id)
        )
    )
    cfg = cfg_res.scalars().first()

    client = BotManager.get_bot(bot_id)
    guild_name = cfg.guild_name if cfg else "Discord Server"
    guild_icon = cfg.guild_icon if cfg else None

    if client:
        g = client.get_guild(int(guild_id))
        if g:
            guild_name = g.name
            if g.icon:
                guild_icon = str(g.icon.url)

    settings = get_settings()

    return templates.TemplateResponse(
        request=request,
        name="verify.html",
        context={
            "bot": bot,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "guild_icon": guild_icon,
            "config": cfg,
            "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
            "hcaptcha_site_key": settings.HCAPTCHA_SITE_KEY,
        },
    )


@router.get("/api/oauth/authorize/{bot_id}/{guild_id}")
async def start_oauth(
    bot_id: int,
    guild_id: str,
    captcha_token: Optional[str] = Query(None),
    ipv4: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    bot_res = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = bot_res.scalar_one_or_none()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    net_info = detect_network_addresses()
    redirect_uri = net_info["redirect_uri"]

    state_payload = {
        "bot_id": bot_id,
        "guild_id": guild_id,
        "captcha_token": captcha_token or "",
        "ipv4": ipv4 or "",
    }
    state_str = base64.urlsafe_b64encode(json.dumps(state_payload).encode()).decode()

    scopes = "identify guilds.join email"
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize?"
        f"client_id={bot.client_id}&"
        f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
        f"response_type=code&"
        f"scope={urllib.parse.quote(scopes)}&"
        f"state={state_str}"
    )

    return RedirectResponse(url=discord_auth_url)


@router.get("/api/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if error or not code or not state:
        return templates.TemplateResponse(
            request=request,
            name="verify_result.html",
            context={
                "success": False,
                "title": "Verification Cancelled",
                "message": f"Discord authorization was denied or cancelled. ({error or 'No code provided'})",
            },
        )

    try:
        state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
    except Exception as e:
        logger.error(f"Invalid state parameter: {e}")
        return templates.TemplateResponse(
            request=request,
            name="verify_result.html",
            context={
                "success": False,
                "title": "Invalid Request",
                "message": "The verification state was invalid or expired. Please try again.",
            },
        )

    if state_data.get("action") == "dashboard_login":
        from web.routes.auth import handle_discord_login_callback
        return await handle_discord_login_callback(code, state_data, db, request)

    bot_id = int(state_data.get("bot_id", 1))
    guild_id = str(state_data.get("guild_id", ""))
    captcha_token = state_data.get("captcha_token")
    state_ipv4 = state_data.get("ipv4") or None

    bot_res = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = bot_res.scalar_one_or_none()
    if not bot:
        return templates.TemplateResponse(
            request=request,
            name="verify_result.html",
            context={"success": False, "title": "Error", "message": "Bot not found."},
        )

    client_secret = decrypt_secret(bot.client_secret_encrypted)
    net_info = detect_network_addresses()
    redirect_uri = net_info["redirect_uri"]

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        token_data = {
            "client_id": bot.client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        token_headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            token_resp = await http_client.post(
                "https://discord.com/api/oauth2/token",
                data=token_data,
                headers=token_headers,
            )
            if token_resp.status_code != 200:
                logger.error(f"Failed to exchange code: {token_resp.text}")
                return templates.TemplateResponse(
                    request=request,
                    name="verify_result.html",
                    context={
                        "success": False,
                        "title": "Authentication Failed",
                        "message": f"Discord token exchange failed (status {token_resp.status_code}).",
                    },
                )
            tokens = token_resp.json()
        except Exception as e:
            logger.error(f"HTTP error during token exchange: {e}")
            return templates.TemplateResponse(
                request=request,
                name="verify_result.html",
                context={"success": False, "title": "Connection Error", "message": str(e)},
            )

        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 604800)

        user_headers = {"Authorization": f"Bearer {access_token}"}
        try:
            user_resp = await http_client.get(
                "https://discord.com/api/users/@me",
                headers=user_headers,
            )
            if user_resp.status_code != 200:
                return templates.TemplateResponse(
                    request=request,
                    name="verify_result.html",
                    context={"success": False, "title": "Error", "message": "Failed to fetch Discord user profile."},
                )
            user_data = user_resp.json()
        except Exception as e:
            return templates.TemplateResponse(
                request=request,
                name="verify_result.html",
                context={"success": False, "title": "Connection Error", "message": str(e)},
            )

    user_id = str(user_data["id"])
    username = user_data.get("username", "Unknown")
    discriminator = user_data.get("discriminator", "0")
    email = user_data.get("email")
    avatar = user_data.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
        if avatar
        else "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    client_ip = get_client_ip(request, client_override=state_ipv4)

    cfg_res = await db.execute(
        select(GuildConfig).where(
            (GuildConfig.guild_id == guild_id) & (GuildConfig.bot_id == bot_id)
        )
    )
    cfg = cfg_res.scalars().first()
    if not cfg:
        cfg = GuildConfig(guild_id=guild_id, bot_id=bot_id, firewall_enabled=False)

    passed, reason, telemetry = await SecurityService.evaluate_member_security(
        db=db,
        guild_config=cfg,
        user_id=user_id,
        client_ip=client_ip,
        captcha_token=captcha_token,
    )

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    user_agent_str = request.headers.get("user-agent", "")
    from core.security import parse_user_agent
    ua_info = parse_user_agent(user_agent_str)

    extra_details = {
        "region": telemetry.get("region", "Unknown"),
        "zip": telemetry.get("zip", ""),
        "timezone": telemetry.get("timezone", "UTC"),
        "lat": telemetry.get("lat"),
        "lon": telemetry.get("lon"),
        "org": telemetry.get("org", telemetry.get("isp", "Unknown")),
        "account_age_days": round(telemetry.get("account_age_days", 0), 1),
        "accept_language": request.headers.get("accept-language", ""),
        "sec_ch_ua_platform": request.headers.get("sec-ch-ua-platform", "").replace('"', ''),
    }

    existing_stmt = select(MemberToken).where(
        (MemberToken.user_id == user_id) & (MemberToken.bot_id == bot_id)
    )
    existing_res = await db.execute(existing_stmt)
    member_record = existing_res.scalars().first()

    if not member_record:
        member_record = MemberToken(
            user_id=user_id,
            username=username,
            discriminator=discriminator,
            email=email,
            avatar=avatar_url,
            access_token_encrypted=encrypt_secret(access_token),
            refresh_token_encrypted=encrypt_secret(refresh_token),
            expires_at=expires_at,
            ip_address=telemetry["ip"],
            country=telemetry["country"],
            country_code=telemetry["country_code"],
            city=telemetry["city"],
            isp=telemetry["isp"],
            asn=str(telemetry["asn"]),
            user_agent=user_agent_str,
            device_os=ua_info["os"],
            device_browser=ua_info["browser"],
            device_type=ua_info["device"],
            extra_info_json=json.dumps(extra_details),
            is_vpn=telemetry["is_vpn"],
            is_cellular=telemetry["is_cellular"],
            is_blacklisted=not passed,
            bot_id=bot_id,
            source_guild_id=guild_id,
        )
        db.add(member_record)
    else:
        member_record.username = username
        member_record.discriminator = discriminator
        member_record.email = email
        member_record.avatar = avatar_url
        member_record.access_token_encrypted = encrypt_secret(access_token)
        member_record.refresh_token_encrypted = encrypt_secret(refresh_token)
        member_record.expires_at = expires_at
        member_record.ip_address = telemetry["ip"]
        member_record.country = telemetry["country"]
        member_record.country_code = telemetry["country_code"]
        member_record.city = telemetry["city"]
        member_record.isp = telemetry["isp"]
        member_record.asn = str(telemetry["asn"])
        member_record.user_agent = user_agent_str
        member_record.device_os = ua_info["os"]
        member_record.device_browser = ua_info["browser"]
        member_record.device_type = ua_info["device"]
        member_record.extra_info_json = json.dumps(extra_details)
        member_record.is_vpn = telemetry["is_vpn"]
        member_record.is_cellular = telemetry["is_cellular"]
        if not passed:
            member_record.is_blacklisted = True
        member_record.source_guild_id = guild_id

    await db.commit()

    if not passed:
        if "blacklisted" in reason.lower() or "banned" in reason.lower():
            try:
                import discord
                client = BotManager.get_bot(bot_id)
                if client and client.is_ready():
                    g = client.get_guild(int(guild_id))
                    if g:
                        await g.ban(discord.Object(id=int(user_id)), reason=f"Blacklisted: {reason}")
            except Exception as ban_err:
                logger.warning(f"Could not auto ban blacklisted user {user_id}: {ban_err}")

        if cfg.webhook_url:
            await WebhookService.log_verification_failed(
                webhook_url=cfg.webhook_url,
                guild_name=cfg.guild_name,
                user_id=user_id,
                username=username,
                reason=reason,
                ip=telemetry["ip"],
                isp=telemetry["isp"],
                country=telemetry["country"],
            )

        await SecurityService.record_audit_log(
            db=db,
            event_type="VERIFY_BLOCKED",
            description=f"Blocked verification for {username} ({user_id}): {reason}",
            guild_id=guild_id,
            user_id=user_id,
            ip_address=telemetry["ip"],
            metadata=telemetry,
        )

        is_vpn_reason = bool(
            "vpn" in reason.lower() or "proxy" in reason.lower() or "datacenter" in reason.lower() or "cellular" in reason.lower()
        )
        return templates.TemplateResponse(
            request=request,
            name="verify_result.html",
            context={
                "success": False,
                "title": "Access Denied",
                "message": reason,
                "is_vpn_block": is_vpn_reason,
                "telemetry": telemetry,
            },
        )

    role_assigned_msg = "Verified status granted."
    role_name = None
    success, role_msg = await BotManager.assign_verified_role(
        bot_db_id=bot_id,
        guild_id=guild_id,
        user_id=user_id,
        role_id=cfg.verified_role_id if cfg and cfg.verified_role_id else None,
        access_token=access_token,
    )
    if success:
        role_assigned_msg = role_msg
        role_name = "Verified"
    else:
        logger.warning(f"Role assignment failed for user {user_id} in guild {guild_id}: {role_msg}")

    if cfg.webhook_url:
        await WebhookService.log_verification_success(
            webhook_url=cfg.webhook_url,
            guild_name=cfg.guild_name,
            user_id=user_id,
            username=username,
            avatar_url=avatar_url,
            ip=telemetry["ip"],
            country=telemetry["country"],
            isp=telemetry["isp"],
            role_name=role_name,
        )

    await SecurityService.record_audit_log(
        db=db,
        event_type="VERIFY_SUCCESS",
        description=f"Verified member {username} ({user_id}) into {cfg.guild_name}",
        guild_id=guild_id,
        user_id=user_id,
        ip_address=telemetry["ip"],
        metadata=telemetry,
    )

    return templates.TemplateResponse(
        request=request,
        name="verify_result.html",
        context={
            "success": True,
            "title": "Verification Successful!",
            "message": f"Welcome, {username}! You have successfully verified into {cfg.guild_name}.",
            "guild_id": guild_id,
            "role_msg": role_assigned_msg,
            "user_data": user_data,
        },
    )
