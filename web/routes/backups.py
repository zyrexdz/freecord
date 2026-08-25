import json
import uuid
import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Bot, Backup, GuildConfig
from web.routes.auth import require_login
from services.bot_manager import BotManager
from services.backup_service import BackupService

logger = logging.getLogger("freecord.web.backups")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/backups", response_class=HTMLResponse)
async def backups_dashboard(
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import get_user_accessible_bot_ids, get_user_bot_permissions
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    if not bot_ids:
        backups = []
    else:
        stmt = select(Backup).where(Backup.bot_id.in_(bot_ids)).order_by(Backup.created_at.desc())
        res = await db.execute(stmt)
        backups = res.scalars().all()

    active_instances = BotManager.get_all_active_bots()
    available_guilds = []

    for bot_id, client in active_instances.items():
        if bot_id in bot_ids and client.is_ready():
            perms = await get_user_bot_permissions(db, current_user, bot_id)
            if not perms.get("can_manage_backups"):
                continue
            allowed_guilds = perms.get("allowed_guilds", ["ALL"])

            for g in client.guilds:
                if "ALL" not in allowed_guilds and str(g.id) not in allowed_guilds:
                    continue
                available_guilds.append({
                    "bot_id": bot_id,
                    "guild_id": str(g.id),
                    "guild_name": g.name,
                    "icon_url": str(g.icon.url) if g.icon else None,
                    "bot_name": client.user.name if client.user else f"Bot {bot_id}",
                })

    return templates.TemplateResponse(
        request=request,
        name="backups.html",
        context={
            "user": current_user,
            "backups": backups,
            "available_guilds": available_guilds,
        },
    )


@router.post("/backups/create")
async def create_backup(
    bot_id: int = Form(...),
    guild_id: str = Form(...),
    include_messages: bool = Form(True),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import get_user_bot_permissions
    perms = await get_user_bot_permissions(db, current_user, bot_id)
    if not perms.get("can_manage_backups"):
        return RedirectResponse(url="/backups?error=You+do+not+have+permission+to+manage+backups", status_code=303)
    client = BotManager.get_bot(bot_id)
    if not client or not client.is_ready():
        return RedirectResponse(url="/backups?error=Selected+bot+is+not+online", status_code=303)

    guild = client.get_guild(int(guild_id))
    if not guild:
        try:
            guild = await client.fetch_guild(int(guild_id))
        except Exception:
            guild = None

    if not guild:
        return RedirectResponse(url="/backups?error=Bot+is+not+in+selected+server", status_code=303)

    try:
        backup = await BackupService.create_guild_backup(
            guild=guild,
            bot_db_id=bot_id,
            db=db,
            include_messages=include_messages,
        )
        return RedirectResponse(url=f"/backups?success=Backup+{backup.backup_uuid}+created", status_code=303)
    except Exception as e:
        logger.error(f"Backup creation error: {e}")
        return RedirectResponse(url=f"/backups?error=Backup+failed:+{str(e)}", status_code=303)


@router.get("/backups/{backup_uuid}/preview")
async def preview_backup(
    backup_uuid: str,
    request: Request,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Backup).where(Backup.backup_uuid == backup_uuid)
    res = await db.execute(stmt)
    backup = res.scalars().first()
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")

    data = json.loads(backup.data_json)
    return templates.TemplateResponse(
        request=request,
        name="backup_preview.html",
        context={
            "user": current_user,
            "backup": backup,
            "data": data,
        },
    )


@router.get("/backups/{backup_uuid}/download")
async def download_backup(
    backup_uuid: str,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Backup).where(Backup.backup_uuid == backup_uuid)
    res = await db.execute(stmt)
    backup = res.scalars().first()
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")

    return Response(
        content=backup.data_json,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=freecord_backup_{backup.backup_uuid}.json"},
    )


@router.post("/backups/{backup_uuid}/restore")
async def restore_backup_route(
    backup_uuid: str,
    request: Request,
    bot_id: int = Form(...),
    target_guild_id: str = Form(...),
    restore_roles: bool = Form(True),
    restore_channels: bool = Form(True),
    restore_emojis: bool = Form(True),
    wipe_first: bool = Form(False),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Backup).where(Backup.backup_uuid == backup_uuid)
    res = await db.execute(stmt)
    backup = res.scalars().first()
    if not backup:
        raise HTTPException(status_code=404, detail="Backup not found")

    client = BotManager.get_bot(bot_id)
    if not client or not client.is_ready():
        return RedirectResponse(url="/backups?error=Selected+bot+is+offline", status_code=303)

    target_guild = client.get_guild(int(target_guild_id))
    if not target_guild:
        try:
            target_guild = await client.fetch_guild(int(target_guild_id))
        except Exception:
            target_guild = None

    if not target_guild:
        return RedirectResponse(url="/backups?error=Bot+is+not+in+target+server", status_code=303)

    restore_id = f"restore_{uuid.uuid4().hex[:8]}"
    data = json.loads(backup.data_json)

    asyncio.create_task(
        BackupService.restore_guild_backup_live(
            restore_id=restore_id,
            guild=target_guild,
            backup_data=data,
            restore_roles=restore_roles,
            restore_channels=restore_channels,
            restore_emojis=restore_emojis,
            wipe_first=wipe_first,
        )
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", ""):
        return JSONResponse({"status": "OK", "restore_id": restore_id, "guild_name": target_guild.name})

    return RedirectResponse(
        url=f"/backups?active_restore={restore_id}&guild_name={target_guild.name}",
        status_code=303,
    )


@router.get("/api/backups/{restore_id}/status")
async def get_backup_restore_status(
    restore_id: str,
    current_user: User = Depends(require_login),
):
    st = BackupService.get_restore_state(restore_id)
    if not st:
        return {"restore_id": restore_id, "status": "PENDING", "progress": 0, "logs": ["Waiting for restore task to start..."]}
    return st


@router.post("/api/backups/{restore_id}/stop")
@router.post("/backups/{restore_id}/stop")
async def stop_backup_restore_endpoint(
    restore_id: str,
    current_user: User = Depends(require_login),
):
    stopped = BackupService.stop_restore(restore_id)
    return {"status": "STOPPED" if stopped else "NOT_FOUND", "restore_id": restore_id}



@router.websocket("/ws/backups/{restore_id}")
async def websocket_backup_restore_endpoint(websocket: WebSocket, restore_id: str):
    await websocket.accept()
    BackupService.register_ws(restore_id, websocket)

    init_state = BackupService.get_restore_state(restore_id)
    if init_state:
        try:
            await websocket.send_json(init_state)
        except Exception:
            pass

    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        BackupService.unregister_ws(restore_id, websocket)


@router.api_route("/backups/{backup_uuid}/delete", methods=["GET", "POST"])
async def delete_backup(
    backup_uuid: str,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Backup).where(Backup.backup_uuid == backup_uuid)
    res = await db.execute(stmt)
    backup = res.scalars().first()
    if backup:
        await db.delete(backup)
        await db.commit()

    return RedirectResponse(url="/backups?success=Backup+deleted", status_code=303)
