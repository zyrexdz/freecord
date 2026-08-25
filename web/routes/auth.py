import logging
import json
import base64
import uuid
import secrets
import urllib.parse
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Bot
from core.security import verify_password, create_access_token, decode_access_token, get_password_hash, decrypt_secret
from core.config import detect_network_addresses

logger = logging.getLogger("freecord.web.auth")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get("freecord_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

    if not token:
        return None

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None

    username = payload["sub"]
    stmt = select(User).where(User.username == username)
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user or not user.is_active:
        return None
    return user


async def require_login(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login?error=Authentication+required"},
        )
    return user


async def require_admin(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await require_login(request, db)
    if user.role != "admin" and user.id != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return user


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    error: Optional[str] = None,
    tab: Optional[str] = "login",
    db: AsyncSession = Depends(get_db),
):
    from database.models import PlatformSetting
    from core.config import get_settings

    oauth_stmt = select(PlatformSetting).where(PlatformSetting.key == "discord_oauth_config")
    oauth_setting = (await db.execute(oauth_stmt)).scalars().first()
    discord_login_enabled = False
    if oauth_setting:
        try:
            cfg = json.loads(oauth_setting.value_json)
            if cfg.get("enabled") and cfg.get("client_id"):
                discord_login_enabled = True
        except Exception:
            pass
    elif get_settings().DISCORD_OAUTH_ENABLED and get_settings().DISCORD_CLIENT_ID:
        discord_login_enabled = True

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": error, "tab": tab, "discord_login_enabled": discord_login_enabled},
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    from database.models import PlatformSetting
    from core.config import get_settings

    oauth_stmt = select(PlatformSetting).where(PlatformSetting.key == "discord_oauth_config")
    oauth_setting = (await db.execute(oauth_stmt)).scalars().first()
    discord_login_enabled = False
    if oauth_setting:
        try:
            cfg = json.loads(oauth_setting.value_json)
            if cfg.get("enabled") and cfg.get("client_id"):
                discord_login_enabled = True
        except Exception:
            pass
    elif get_settings().DISCORD_OAUTH_ENABLED and get_settings().DISCORD_CLIENT_ID:
        discord_login_enabled = True

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": error, "tab": "register", "discord_login_enabled": discord_login_enabled},
    )


@router.get("/auth/discord/login")
async def discord_login_start(request: Request, db: AsyncSession = Depends(get_db)):
    from database.models import PlatformSetting
    from core.config import get_settings

    client_id = None
    oauth_stmt = select(PlatformSetting).where(PlatformSetting.key == "discord_oauth_config")
    oauth_setting = (await db.execute(oauth_stmt)).scalars().first()
    if oauth_setting:
        try:
            cfg = json.loads(oauth_setting.value_json)
            if cfg.get("enabled") and cfg.get("client_id"):
                client_id = cfg.get("client_id")
        except Exception:
            pass

    if not client_id:
        s = get_settings()
        if s.DISCORD_OAUTH_ENABLED and s.DISCORD_CLIENT_ID:
            client_id = s.DISCORD_CLIENT_ID

    if not client_id:
        return RedirectResponse(
            url="/login?error=Discord+login+is+not+enabled.+Please+sign+in+and+configure+it+in+Settings.",
            status_code=303,
        )

    net_info = detect_network_addresses()
    redirect_uri = net_info["redirect_uri"]

    state_payload = {
        "action": "dashboard_login",
        "csrf": uuid.uuid4().hex,
        "redirect_uri": redirect_uri,
    }
    state_encoded = base64.urlsafe_b64encode(json.dumps(state_payload).encode()).decode()

    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&response_type=code"
        f"&scope=identify+email"
        f"&state={state_encoded}"
        f"&prompt=consent"
    )
    return RedirectResponse(url=discord_auth_url, status_code=302)


async def handle_discord_login_callback(
    code: str,
    state_data: dict,
    db: AsyncSession,
    request: Request,
) -> RedirectResponse:
    from database.models import PlatformSetting
    from core.config import get_settings

    client_id = None
    client_secret = None
    oauth_stmt = select(PlatformSetting).where(PlatformSetting.key == "discord_oauth_config")
    oauth_setting = (await db.execute(oauth_stmt)).scalars().first()
    if oauth_setting:
        try:
            cfg = json.loads(oauth_setting.value_json)
            client_id = cfg.get("client_id")
            client_secret = cfg.get("client_secret")
        except Exception:
            pass

    if not client_id or not client_secret:
        s = get_settings()
        client_id = client_id or s.DISCORD_CLIENT_ID
        client_secret = client_secret or s.DISCORD_CLIENT_SECRET

    if not client_id or not client_secret:
        bot_res = await db.execute(select(Bot).order_by(Bot.id.asc()))
        bot = bot_res.scalars().first()
        if bot:
            client_id = bot.client_id
            client_secret = decrypt_secret(bot.client_secret_encrypted)

    if not client_id or not client_secret:
        return RedirectResponse(url="/login?error=Discord+login+credentials+not+configured", status_code=303)

    redirect_uri = state_data.get("redirect_uri")
    if not redirect_uri:
        net_info = detect_network_addresses()
        redirect_uri = net_info["redirect_uri"]

    token_payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        try:
            token_resp = await http_client.post(
                "https://discord.com/api/oauth2/token",
                data=token_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except Exception as e:
            logger.error(f"Discord login HTTP error: {e}")
            return RedirectResponse(url="/login?error=Failed+to+connect+to+Discord", status_code=303)

        if token_resp.status_code != 200:
            logger.error(f"Discord login token exchange failed: {token_resp.text}")
            return RedirectResponse(url="/login?error=Discord+login+failed.+Check+redirect+URL", status_code=303)

        token_json = token_resp.json()
        access_token = token_json.get("access_token")
        if not access_token:
            return RedirectResponse(url="/login?error=Invalid+Discord+token+response", status_code=303)

        try:
            user_resp = await http_client.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except Exception as e:
            return RedirectResponse(url="/login?error=Failed+to+fetch+user+profile", status_code=303)

        if user_resp.status_code != 200:
            return RedirectResponse(url="/login?error=Could+not+retrieve+Discord+profile", status_code=303)

        d_user = user_resp.json()

    discord_user_id = str(d_user.get("id"))
    discord_username = d_user.get("username") or f"discord_{discord_user_id[:6]}"
    avatar_hash = d_user.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png"
        if avatar_hash else None
    )
    email = d_user.get("email")

    user_stmt = select(User).where(User.discord_id == discord_user_id)
    user = (await db.execute(user_stmt)).scalars().first()

    if not user:
        user_stmt = select(User).where(User.username == discord_username)
        user = (await db.execute(user_stmt)).scalars().first()
        if user:
            user.discord_id = discord_user_id
            if avatar_url:
                user.avatar_url = avatar_url
            if email and not user.email:
                user.email = email
            await db.commit()

    if not user:
        all_users = (await db.execute(select(User))).scalars().all()
        role = "admin" if len(all_users) == 0 else "manager"
        user = User(
            username=discord_username,
            password_hash=f"DISCORD_OAUTH_{secrets.token_hex(16)}",
            discord_id=discord_user_id,
            avatar_url=avatar_url,
            email=email,
            role=role,
            is_active=True,
        )
        db.add(user)
        await db.commit()
    else:
        if avatar_url:
            user.avatar_url = avatar_url
        if email and not user.email:
            user.email = email
        await db.commit()

    if not user.is_active:
        return RedirectResponse(url="/login?error=Your+account+is+disabled", status_code=303)

    token = create_access_token({"sub": user.username, "role": user.role})
    redirect_resp = RedirectResponse(url="/?success=Signed+in+with+Discord", status_code=303)
    redirect_resp.set_cookie(
        key="freecord_token",
        value=token,
        httponly=True,
        max_age=60 * 60 * 24 * 7,
        samesite="lax",
        path="/",
    )
    return redirect_resp


@router.get("/auth/discord/callback")
async def discord_login_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if error or not code:
        return RedirectResponse(url=f"/login?error=Discord+login+cancelled", status_code=303)

    state_data = {}
    if state:
        try:
            state_data = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        except Exception:
            pass

    return await handle_discord_login_callback(code, state_data, db, request)


@router.post("/login")
async def login_submit(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.username == username.strip())
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse(url="/login?error=Invalid+username+or+password", status_code=303)

    if not user.is_active:
        return RedirectResponse(url="/login?error=Account+is+disabled", status_code=303)

    token = create_access_token({"sub": user.username, "role": user.role})
    redirect_resp = RedirectResponse(url="/", status_code=303)
    redirect_resp.set_cookie(
        key="freecord_token",
        value=token,
        httponly=True,
        max_age=60 * 60 * 24 * 7,
        samesite="lax",
        path="/",
    )
    return redirect_resp


@router.post("/register")
async def register_submit(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    uname = username.strip()
    if len(uname) < 3:
        return RedirectResponse(url="/register?error=Username+must+be+at+least+3+characters", status_code=303)

    if password != confirm_password:
        return RedirectResponse(url="/register?error=Passwords+do+not+match", status_code=303)

    if len(password) < 6:
        return RedirectResponse(url="/register?error=Password+must+be+at+least+6+characters", status_code=303)

    stmt = select(User).where(User.username == uname)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        return RedirectResponse(url="/register?error=Username+already+taken", status_code=303)

    all_users = await db.execute(select(User))
    user_count = len(all_users.scalars().all())
    role = "admin" if user_count == 0 else "manager"

    new_user = User(
        username=uname,
        password_hash=get_password_hash(password),
        role=role,
        is_active=True,
    )
    db.add(new_user)
    await db.commit()

    token = create_access_token({"sub": new_user.username, "role": new_user.role})
    redirect_resp = RedirectResponse(url="/?success=Account+created+successfully", status_code=303)
    redirect_resp.set_cookie(
        key="freecord_token",
        value=token,
        httponly=True,
        max_age=60 * 60 * 24 * 7,
        samesite="lax",
        path="/",
    )
    return redirect_resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("freecord_token", path="/")
    return resp


@router.post("/api/staff/create")
async def create_staff_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("manager"),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role != "admin" and current_user.id != 1:
        raise HTTPException(status_code=403, detail="Admin role required")

    stmt = select(User).where(User.username == username)
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        return RedirectResponse(url="/settings?error=Username+already+exists", status_code=303)

    new_user = User(
        username=username.strip(),
        password_hash=get_password_hash(password),
        role=role.strip(),
        is_active=True,
    )
    db.add(new_user)
    await db.commit()
    return RedirectResponse(url="/settings?success=Staff+user+created", status_code=303)


@router.post("/api/staff/update-role")
async def update_staff_role(
    user_id: int = Form(...),
    role: str = Form(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role != "admin" and current_user.id != 1:
        raise HTTPException(status_code=403, detail="Admin role required")

    stmt = select(User).where(User.id == user_id)
    target_user = (await db.execute(stmt)).scalars().first()
    if not target_user:
        return RedirectResponse(url="/settings?error=User+not+found", status_code=303)

    if target_user.id == current_user.id and role != "admin":
        admin_count = len((await db.execute(select(User).where(User.role == "admin"))).scalars().all())
        if admin_count <= 1:
            return RedirectResponse(url="/settings?error=Cannot+demote+the+only+admin+account", status_code=303)

    target_user.role = role.strip()
    db.add(target_user)
    await db.commit()
    return RedirectResponse(url="/settings?success=User+role+updated+successfully", status_code=303)


@router.post("/api/staff/delete")
async def delete_staff_user(
    user_id: int = Form(...),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role != "admin" and current_user.id != 1:
        raise HTTPException(status_code=403, detail="Admin role required")

    if user_id == current_user.id:
        return RedirectResponse(url="/settings?error=You+cannot+delete+your+own+account", status_code=303)

    stmt = select(User).where(User.id == user_id)
    target_user = (await db.execute(stmt)).scalars().first()
    if target_user:
        await db.delete(target_user)
        await db.commit()

    return RedirectResponse(url="/settings?success=User+deleted+successfully", status_code=303)
