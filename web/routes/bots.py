import json
import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Bot, GuildConfig, MemberToken, Backup, PullTask, BotCollaborator
from web.routes.auth import require_login
from core.security import encrypt_secret, decrypt_secret
from services.bot_manager import BotManager
from core.config import detect_network_addresses

logger = logging.getLogger("freecord.web.bots")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/bots/new", response_class=HTMLResponse)
async def new_bot_page(
    request: Request,
    current_user: User = Depends(require_login),
):
    net_info = detect_network_addresses()
    return templates.TemplateResponse(
        request=request,
        name="add_bot.html",
        context={"user": current_user, "net_info": net_info},
    )


@router.get("/bots/sync")
async def sync_all_bots(
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Bot).where(Bot.is_active == True)
    res = await db.execute(stmt)
    bots = res.scalars().all()
    for b in bots:
        client = BotManager.get_bot(b.id)
        if not client or not client.is_ready():
            await BotManager.start_bot(b.id)
        else:
            for guild in client.guilds:
                try:
                    await BotManager.ensure_verified_role(b.id, guild)
                except Exception as e:
                    logger.warning(f"Error auto configuring role for {guild.name}: {e}")
    return RedirectResponse(url="/bots?success=All+bots+and+servers+refreshed", status_code=303)


@router.post("/bots/{bot_id}/sync")
@router.get("/bots/{bot_id}/sync")
async def sync_single_bot(
    bot_id: int,
    current_user: User = Depends(require_login),
):
    client = BotManager.get_bot(bot_id)
    if client and client.is_ready():
        for guild in client.guilds:
            try:
                await BotManager.ensure_verified_role(bot_id, guild)
            except Exception as e:
                logger.warning(f"Error auto configuring role for {guild.name}: {e}")
        return RedirectResponse(url=f"/bots?success=Bot+synced+and+new+servers+detected", status_code=303)
    else:
        await BotManager.start_bot(bot_id)
        return RedirectResponse(url=f"/bots?success=Bot+connecting+to+Discord", status_code=303)


@router.get("/bots", response_class=HTMLResponse)
async def list_bots(
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import get_user_accessible_bot_ids
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    if not bot_ids:
        bots = []
    else:
        stmt = select(Bot).where(Bot.id.in_(bot_ids)).order_by(Bot.created_at.desc())
        res = await db.execute(stmt)
        bots = res.scalars().all()

    bot_cards = []
    active_instances = BotManager.get_all_active_bots()
    net_info = detect_network_addresses()

    all_users = (await db.execute(select(User))).scalars().all()

    for b in bots:
        if b.is_active and (b.id not in active_instances or not active_instances[b.id].is_ready()):
            if b.id not in BotManager._bot_tasks or BotManager._bot_tasks[b.id].done():
                await BotManager.start_bot(b.id)

        client = active_instances.get(b.id)
        is_live = client is not None and client.is_ready()
        
        guilds_list = []
        if is_live:
            guilds_list = [{"id": str(g.id), "name": g.name} for g in client.guilds]
        else:
            cfg_stmt = select(GuildConfig).where(GuildConfig.bot_id == b.id)
            configs = (await db.execute(cfg_stmt)).scalars().all()
            guilds_list = [{"id": g.guild_id, "name": g.guild_name} for g in configs]

        guilds_count = len(guilds_list)

        tok_stmt = select(func.count(MemberToken.id)).where(MemberToken.bot_id == b.id)
        tok_res = await db.execute(tok_stmt)
        token_count = tok_res.scalar() or 0

        collab_stmt = select(BotCollaborator).where(BotCollaborator.bot_id == b.id)
        collab_res = await db.execute(collab_stmt)
        collaborators = list(collab_res.scalars().all())

        collabs_data = []
        for c in collaborators:
            try:
                allowed_guilds = json.loads(c.allowed_guilds_json) if c.allowed_guilds_json else ["ALL"]
            except Exception:
                allowed_guilds = ["ALL"]

            collabs_data.append({
                "id": c.id,
                "username": c.username,
                "role_label": c.role_label,
                "can_manage_backups": c.can_manage_backups,
                "can_start_migrations": c.can_start_migrations,
                "can_view_member_details": c.can_view_member_details,
                "can_manage_blacklist": c.can_manage_blacklist,
                "can_manage_settings": c.can_manage_settings,
                "can_export_tokens": c.can_export_tokens,
                "allowed_guilds": allowed_guilds,
                "created_at": c.created_at.strftime("%b %d, %Y") if c.created_at else "Recently",
            })

        invite_url = (
            f"https://discord.com/oauth2/authorize?client_id={b.client_id}&permissions=8&scope=bot%20applications.commands"
        )

        bot_cards.append({
            "record": b,
            "is_live": is_live,
            "guilds_count": guilds_count,
            "guilds": guilds_list,
            "token_count": token_count,
            "collaborators": collabs_data,
            "collaborators_count": len(collabs_data),
            "invite_url": invite_url,
            "client_secret_masked": "••••••••••••••••",
        })

    return templates.TemplateResponse(
        request=request,
        name="bots.html",
        context={
            "user": current_user,
            "bot_cards": bot_cards,
            "all_users": all_users,
            "net_info": net_info,
        },
    )


@router.post("/bots/add")
async def add_bot(
    name: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    token: str = Form(...),
    custom_status: Optional[str] = Form("FreeCord Security & Backup"),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Bot).where(Bot.client_id == client_id.strip())
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        return RedirectResponse(url="/bots?error=Bot+with+this+Client+ID+already+exists", status_code=303)

    new_bot = Bot(
        name=name.strip(),
        client_id=client_id.strip(),
        client_secret_encrypted=encrypt_secret(client_secret.strip()),
        token_encrypted=encrypt_secret(token.strip()),
        custom_status=custom_status.strip() if custom_status else "FreeCord Security & Backup",
        owner_id=current_user.id,
        status="CONNECTING",
        is_active=True,
    )
    db.add(new_bot)
    await db.commit()
    await db.refresh(new_bot)

    await BotManager.start_bot(new_bot.id)

    return RedirectResponse(url="/bots?success=Bot+added+successfully", status_code=303)


@router.post("/bots/{bot_id}/toggle")
async def toggle_bot(
    bot_id: int,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Bot).where(Bot.id == bot_id)
    res = await db.execute(stmt)
    bot = res.scalar_one_or_none()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    client = BotManager.get_bot(bot_id)
    if client and client.is_ready():
        await BotManager.stop_bot(bot_id)
        bot.is_active = False
    else:
        bot.is_active = True
        await BotManager.start_bot(bot_id)

    await db.commit()
    return RedirectResponse(url="/bots", status_code=303)


@router.post("/bots/{bot_id}/restart")
async def restart_bot(
    bot_id: int,
    current_user: User = Depends(require_login),
):
    await BotManager.restart_bot(bot_id)
    return RedirectResponse(url="/bots?success=Bot+restarted", status_code=303)


@router.post("/bots/{bot_id}/delete")
@router.api_route("/bots/{bot_id}/delete", methods=["GET", "POST"])
async def delete_bot(
    bot_id: int,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import can_user_access_bot
    if not await can_user_access_bot(db, current_user, bot_id):
        return RedirectResponse(url="/bots?error=You+do+not+have+permission+to+delete+this+bot", status_code=303)

    try:
        await BotManager.stop_bot(bot_id)
    except Exception as e:
        logger.warning(f"Error stopping bot {bot_id} during delete: {e}")

    try:
        from database.models import BotCollaborator
        await db.execute(delete(GuildConfig).where(GuildConfig.bot_id == bot_id))
        await db.execute(delete(Backup).where(Backup.bot_id == bot_id))
        await db.execute(delete(MemberToken).where(MemberToken.bot_id == bot_id))
        await db.execute(delete(PullTask).where(PullTask.bot_id == bot_id))
        await db.execute(delete(BotCollaborator).where(BotCollaborator.bot_id == bot_id))
        await db.execute(delete(Bot).where(Bot.id == bot_id))
        await db.commit()
        return RedirectResponse(url="/bots?success=Bot+deleted+successfully", status_code=303)
    except Exception as err:
        logger.error(f"Error deleting bot {bot_id}: {err}")
        await db.rollback()
        return RedirectResponse(url="/bots?error=Failed+to+delete+bot", status_code=303)


@router.post("/bots/{bot_id}/collaborators/add")

async def add_bot_collaborator(
    bot_id: int,
    username: str = Form(...),
    role_label: str = Form("Helper"),
    can_manage_backups: bool = Form(False),
    can_start_migrations: bool = Form(False),
    can_view_member_details: bool = Form(False),
    can_manage_blacklist: bool = Form(False),
    can_manage_settings: bool = Form(False),
    can_export_tokens: bool = Form(False),
    allowed_guilds: List[str] = Form(["ALL"]),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    clean_username = username.strip()
    if not clean_username:
        return RedirectResponse(url="/bots?error=Username+is+required", status_code=303)

    user_match = (await db.execute(select(User).where(User.username == clean_username))).scalar_one_or_none()
    user_id = user_match.id if user_match else None

    existing = (await db.execute(
        select(BotCollaborator).where(
            (BotCollaborator.bot_id == bot_id) & (BotCollaborator.username == clean_username)
        )
    )).scalars().first()

    guilds_json = json.dumps(allowed_guilds)

    if existing:
        existing.role_label = role_label
        existing.can_manage_backups = can_manage_backups
        existing.can_start_migrations = can_start_migrations
        existing.can_view_member_details = can_view_member_details
        existing.can_manage_blacklist = can_manage_blacklist
        existing.can_manage_settings = can_manage_settings
        existing.can_export_tokens = can_export_tokens
        existing.allowed_guilds_json = guilds_json
        existing.user_id = user_id
    else:
        new_collab = BotCollaborator(
            bot_id=bot_id,
            username=clean_username,
            user_id=user_id,
            role_label=role_label,
            can_manage_backups=can_manage_backups,
            can_start_migrations=can_start_migrations,
            can_view_member_details=can_view_member_details,
            can_manage_blacklist=can_manage_blacklist,
            can_manage_settings=can_manage_settings,
            can_export_tokens=can_export_tokens,
            allowed_guilds_json=guilds_json,
        )
        db.add(new_collab)

    await db.commit()
    return RedirectResponse(url=f"/bots?success=Team+member+{clean_username}+permissions+saved", status_code=303)


@router.post("/bots/{bot_id}/collaborators/{collab_id}/delete")
async def delete_bot_collaborator(
    bot_id: int,
    collab_id: int,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(delete(BotCollaborator).where(
        (BotCollaborator.id == collab_id) & (BotCollaborator.bot_id == bot_id)
    ))
    await db.commit()
    return RedirectResponse(url="/bots?success=Team+member+removed", status_code=303)


@router.get("/api/bots/{bot_id}/collaborators")
async def get_bot_collaborators_api(
    bot_id: int,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(BotCollaborator).where(BotCollaborator.bot_id == bot_id)
    collabs = (await db.execute(stmt)).scalars().all()
    results = []
    for c in collabs:
        try:
            guilds = json.loads(c.allowed_guilds_json) if c.allowed_guilds_json else ["ALL"]
        except Exception:
            guilds = ["ALL"]
        results.append({
            "id": c.id,
            "username": c.username,
            "role_label": c.role_label,
            "can_manage_backups": c.can_manage_backups,
            "can_start_migrations": c.can_start_migrations,
            "can_view_member_details": c.can_view_member_details,
            "can_manage_blacklist": c.can_manage_blacklist,
            "can_manage_settings": c.can_manage_settings,
            "can_export_tokens": c.can_export_tokens,
            "allowed_guilds": guilds,
        })
    return JSONResponse(results)
