import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Set
import aiohttp
from sqlalchemy import select, update

from database.session import async_session_factory
from database.models import PullTask, MemberToken, Bot, GuildConfig
from core.security import decrypt_secret, encrypt_secret
from core.proxy_manager import proxy_manager
from services.webhook_service import WebhookService

logger = logging.getLogger("freecord.migration_engine")

_engine_tasks: Dict[str, Dict[str, Any]] = {}
_ws_subscribers: Dict[str, Set[Any]] = {}
_latest_states: Dict[str, Dict[str, Any]] = {}


class MigrationEngine:

    @staticmethod
    def register_ws(task_uuid: str, ws: Any):
        if task_uuid not in _ws_subscribers:
            _ws_subscribers[task_uuid] = set()
        _ws_subscribers[task_uuid].add(ws)

    @staticmethod
    def unregister_ws(task_uuid: str, ws: Any):
        if task_uuid in _ws_subscribers:
            _ws_subscribers[task_uuid].discard(ws)
            if not _ws_subscribers[task_uuid]:
                del _ws_subscribers[task_uuid]

    @staticmethod
    def get_state(task_uuid: str) -> Optional[Dict[str, Any]]:
        return _latest_states.get(task_uuid)

    @staticmethod
    async def broadcast(task_uuid: str, payload: Dict[str, Any]):
        _latest_states[task_uuid] = payload
        subs = _ws_subscribers.get(task_uuid, set())
        dead = []
        for ws in list(subs):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for d in dead:
            subs.discard(d)

    @classmethod
    async def start(cls, task_uuid: str, limit_count: Optional[int] = None, min_stay_days: int = 0):
        if task_uuid in _engine_tasks and _engine_tasks[task_uuid].get("running"):
            logger.info(f"Task {task_uuid} is already running.")
            return

        stop_event = asyncio.Event()
        pause_event = asyncio.Event()
        pause_event.set()

        _engine_tasks[task_uuid] = {
            "running": True,
            "stop_event": stop_event,
            "pause_event": pause_event,
            "started_at": time.time(),
        }

        asyncio.create_task(cls._worker(task_uuid, stop_event, pause_event, limit_count, min_stay_days))

    @classmethod
    def pause(cls, task_uuid: str):
        if task_uuid in _engine_tasks:
            _engine_tasks[task_uuid]["pause_event"].clear()

    @classmethod
    def resume(cls, task_uuid: str):
        if task_uuid in _engine_tasks:
            _engine_tasks[task_uuid]["pause_event"].set()

    @classmethod
    def stop(cls, task_uuid: str):
        if task_uuid in _engine_tasks:
            _engine_tasks[task_uuid]["stop_event"].set()
            _engine_tasks[task_uuid]["pause_event"].set()

    @classmethod
    async def _refresh_oauth_token(
        cls,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        proxy_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        url = "https://discord.com/api/v10/oauth2/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            async with session.post(url, data=data, headers=headers, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"OAuth refresh failed with status {resp.status}")
        except Exception as e:
            logger.warning(f"OAuth refresh error: {e}")
        return None

    @classmethod
    async def _worker(
        cls,
        task_uuid: str,
        stop_event: asyncio.Event,
        pause_event: asyncio.Event,
        limit_count: Optional[int] = None,
        min_stay_days: int = 0,
    ):
        logger.info(f"Migration worker launched for task {task_uuid}")
        start_time = time.time()
        milestones_sent = set()
        logs_list = []

        def cur_time():
            return datetime.now().strftime("%H:%M:%S")

        try:
            async with async_session_factory() as db:
                stmt = select(PullTask).where(PullTask.task_uuid == task_uuid)
                res = await db.execute(stmt)
                task = res.scalar_one_or_none()
                if not task:
                    return

                task.status = "RUNNING"
                await db.commit()

                bot_stmt = select(Bot).where(Bot.id == task.bot_id)
                bot_res = await db.execute(bot_stmt)
                bot = bot_res.scalar_one_or_none()
                if not bot:
                    task.status = "FAILED"
                    task.error_message = "Selected bot not found"
                    await db.commit()
                    await cls.broadcast(task_uuid, {
                        "status": "FAILED",
                        "error": "Selected bot not found",
                        "logs": logs_list,
                    })
                    return

                bot_token = decrypt_secret(bot.token_encrypted)
                client_secret = decrypt_secret(bot.client_secret_encrypted)
                client_id = bot.client_id

                guild_stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == task.target_guild_id) & (GuildConfig.bot_id == task.bot_id)
                )
                guild_res = await db.execute(guild_stmt)
                guild_cfg = guild_res.scalars().first()
                if not guild_cfg:
                    guild_stmt = select(GuildConfig).where(GuildConfig.guild_id == task.target_guild_id)
                    guild_res = await db.execute(guild_stmt)
                    guild_cfg = guild_res.scalars().first()

                webhook_url = guild_cfg.webhook_url if guild_cfg else None
                guild_name = guild_cfg.guild_name if guild_cfg and guild_cfg.guild_name else None

                if not guild_name or guild_name == "Discord Guild":
                    from services.bot_manager import BotManager
                    client = BotManager.get_bot(task.bot_id)
                    if client:
                        try:
                            g = client.get_guild(int(task.target_guild_id))
                            if g and g.name:
                                guild_name = g.name
                        except Exception:
                            pass

                if not guild_name:
                    guild_name = f"Server {task.target_guild_id}"

                token_stmt = select(MemberToken).where(
                    (MemberToken.bot_id == task.bot_id) & (MemberToken.is_blacklisted == False)
                ).order_by(MemberToken.verified_at.desc())

                tokens_res = await db.execute(token_stmt)
                all_tokens = list(tokens_res.scalars().all())

                now_utc = datetime.utcnow()
                min_stay = getattr(task, "min_stay_days", 0) or 0
                if min_stay > 0:
                    cutoff = now_utc - timedelta(days=min_stay)
                    all_tokens = [m for m in all_tokens if m.verified_at and m.verified_at <= cutoff]

                if limit_count and limit_count > 0:
                    member_tokens = all_tokens[:limit_count]
                else:
                    member_tokens = all_tokens

                total_members = len(member_tokens)
                task.total_members = total_members
                await db.commit()

                if total_members == 0:
                    task.status = "COMPLETED"
                    msg = f"[{cur_time()}] No verified members available to restore."
                    logs_list.append(msg)
                    task.logs = json.dumps(logs_list)
                    await db.commit()
                    await cls.broadcast(task_uuid, {
                        "status": "COMPLETED",
                        "progress": 100,
                        "processed": 0,
                        "total": 0,
                        "success": 0,
                        "already": 0,
                        "already_in": 0,
                        "failed": 0,
                        "rate_limits": 0,
                        "rate_limited": 0,
                        "speed": 0.0,
                        "logs": logs_list,
                    })
                    return

            logs_list.append(f"[{cur_time()}] Starting restore of {total_members} member(s) into {guild_name}...")
            await cls.broadcast(task_uuid, {
                "status": "RUNNING",
                "progress": 0,
                "processed": 0,
                "total": total_members,
                "success": 0,
                "already": 0,
                "already_in": 0,
                "failed": 0,
                "rate_limits": 0,
                "rate_limited": 0,
                "speed": 0.0,
                "logs": logs_list,
            })

            if not proxy_manager.custom_pool and not proxy_manager.free_pool:
                asyncio.create_task(proxy_manager.initialize())

            success = 0
            failed = 0
            already_in = 0
            rate_limited = 0
            batch_counter = 0

            async with aiohttp.ClientSession() as session:
                for idx, m_token in enumerate(member_tokens, 1):
                    if stop_event.is_set():
                        logger.info(f"Task {task_uuid} stopped by user.")
                        logs_list.append(f"[{cur_time()}] Restore task cancelled by user.")
                        async with async_session_factory() as db:
                            await db.execute(update(PullTask).where(PullTask.task_uuid == task_uuid).values(status="STOPPED", logs=json.dumps(logs_list[-100:])))
                            await db.commit()
                        await cls.broadcast(task_uuid, {"status": "STOPPED", "logs": logs_list[-40:], "message": "Migration task stopped."})
                        return

                    await pause_event.wait()

                    user_id = m_token.user_id
                    username = m_token.username or user_id
                    access_token = decrypt_secret(m_token.access_token_encrypted)
                    refresh_tok = decrypt_secret(m_token.refresh_token_encrypted) if m_token.refresh_token_encrypted else None

                    proxy_url = await proxy_manager.get_next_proxy() if getattr(task, 'use_proxies', False) else None

                    now_utc = datetime.utcnow()
                    if m_token.expires_at and m_token.expires_at <= now_utc + timedelta(seconds=60) and refresh_tok:
                        logs_list.append(f"[{cur_time()}] Refreshing OAuth token for {username}...")
                        await cls.broadcast(task_uuid, {
                            "status": "RUNNING",
                            "logs": logs_list[-40:],
                            "total": total_members,
                            "success": success,
                            "already": already_in,
                            "already_in": already_in,
                            "failed": failed,
                            "rate_limits": rate_limited,
                            "rate_limited": rate_limited,
                        })
                        refreshed = await cls._refresh_oauth_token(session, client_id, client_secret, refresh_tok, proxy_url)
                        if refreshed and "access_token" in refreshed:
                            access_token = refreshed["access_token"]
                            new_refresh = refreshed.get("refresh_token", refresh_tok)
                            new_exp = datetime.utcnow() + timedelta(seconds=refreshed.get("expires_in", 604800))
                            async with async_session_factory() as db:
                                await db.execute(
                                    update(MemberToken)
                                    .where(MemberToken.id == m_token.id)
                                    .values(
                                        access_token_encrypted=encrypt_secret(access_token),
                                        refresh_token_encrypted=encrypt_secret(new_refresh),
                                        expires_at=new_exp,
                                    )
                                )
                                await db.commit()
                        else:
                            failed += 1
                            logs_list.append(f"[{cur_time()}] {username} (<@{user_id}>) revoked app authorization or token expired.")
                            await cls.broadcast(task_uuid, {
                                "status": "RUNNING",
                                "processed": idx,
                                "total": total_members,
                                "progress": int((idx / total_members) * 100),
                                "success": success,
                                "already": already_in,
                                "already_in": already_in,
                                "failed": failed,
                                "rate_limits": rate_limited,
                                "rate_limited": rate_limited,
                                "logs": logs_list[-40:],
                            })
                            continue

                    put_url = f"https://discord.com/api/v10/guilds/{task.target_guild_id}/members/{user_id}"
                    headers = {
                        "Authorization": f"Bot {bot_token}",
                        "Content-Type": "application/json",
                    }
                    body = json.dumps({"access_token": access_token})

                    attempt = 0
                    max_attempts = 4
                    member_done = False

                    while attempt < max_attempts and not member_done and not stop_event.is_set():
                        attempt += 1
                        try:
                            async with session.put(put_url, headers=headers, data=body, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                                if resp.status == 201:
                                    success += 1
                                    member_done = True
                                    logs_list.append(f"[{cur_time()}] Added {username} (<@{user_id}>) to {guild_name}")
                                elif resp.status == 204:
                                    already_in += 1
                                    member_done = True
                                    logs_list.append(f"[{cur_time()}] {username} (<@{user_id}>) is already in server")
                                elif resp.status == 429:
                                    rate_limited += 1
                                    try:
                                        resp_data = await resp.json()
                                        retry_after = float(resp_data.get("retry_after", 2.0))
                                    except Exception:
                                        retry_after = 2.5
                                    sleep_time = retry_after + 0.35
                                    logs_list.append(f"[{cur_time()}] Discord rate limit. Pausing for {sleep_time:.1f}s...")
                                    await cls.broadcast(task_uuid, {
                                        "status": "RATE_LIMITED",
                                        "retry_after": sleep_time,
                                        "logs": logs_list[-40:],
                                        "total": total_members,
                                        "success": success,
                                        "already": already_in,
                                        "already_in": already_in,
                                        "failed": failed,
                                        "rate_limits": rate_limited,
                                        "rate_limited": rate_limited,
                                    })
                                    await asyncio.sleep(sleep_time)
                                    proxy_url = await proxy_manager.get_next_proxy() if getattr(task, 'use_proxies', False) else None
                                elif resp.status == 400:
                                    failed += 1
                                    member_done = True
                                    try:
                                        err_data = await resp.json()
                                        err_code = err_data.get("code", 0)
                                        err_msg = err_data.get("message", "")
                                        if err_code == 30001 or "Maximum number of guilds" in err_msg:
                                            logs_list.append(f"[{cur_time()}] {username} (<@{user_id}>) is at max 100 server limit.")
                                        elif err_code == 50025 or "Invalid OAuth2" in err_msg:
                                            logs_list.append(f"[{cur_time()}] {username} (<@{user_id}>) revoked OAuth token.")
                                        else:
                                            logs_list.append(f"[{cur_time()}] Error adding {username} (<@{user_id}>): {err_msg}")
                                    except Exception:
                                        logs_list.append(f"[{cur_time()}] Failed adding {username} (<@{user_id}>)")
                                elif resp.status == 401:
                                    failed += 1
                                    member_done = True
                                    logs_list.append(f"[{cur_time()}] {username} (<@{user_id}>) revoked application authorization.")
                                elif resp.status == 403:
                                    failed += 1
                                    member_done = True
                                    try:
                                        err_data = await resp.json()
                                        err_code = err_data.get("code", 0)
                                        if err_code == 50013:
                                            logs_list.append(f"[{cur_time()}] Bot lacks permission in {guild_name}.")
                                        else:
                                            logs_list.append(f"[{cur_time()}] Cannot add {username} (<@{user_id}>): User banned or restricted.")
                                    except Exception:
                                        logs_list.append(f"[{cur_time()}] Bot cannot add {username} (<@{user_id}>)")
                                else:
                                    failed += 1
                                    member_done = True
                                    logs_list.append(f"[{cur_time()}] Discord API error {resp.status} adding {username} (<@{user_id}>)")
                        except Exception as err:
                            logger.warning(f"Error migrating member {user_id}: {err}")
                            failed += 1
                            logs_list.append(f"[{cur_time()}] Network error {username}: {str(err)}")
                            member_done = True

                    processed = idx
                    progress_pct = int((processed / total_members) * 100)
                    elapsed = max(time.time() - start_time, 0.1)
                    speed = round(processed / elapsed, 2)
                    remaining = total_members - processed
                    eta_seconds = int(remaining / max(speed, 0.1))

                    for m in (25, 50, 75, 100):
                        if progress_pct >= m and m not in milestones_sent:
                            milestones_sent.add(m)
                            if webhook_url:
                                asyncio.create_task(
                                    WebhookService.log_migration_milestone(
                                        webhook_url=webhook_url,
                                        guild_name=guild_name,
                                        percentage=m,
                                        success=success,
                                        failed=failed,
                                        total=total_members,
                                        status="RUNNING" if m < 100 else "COMPLETED",
                                    )
                                )

                    await cls.broadcast(task_uuid, {
                        "status": "RUNNING",
                        "progress": progress_pct,
                        "processed": processed,
                        "total": total_members,
                        "success": success,
                        "already": already_in,
                        "already_in": already_in,
                        "failed": failed,
                        "rate_limits": rate_limited,
                        "rate_limited": rate_limited,
                        "speed": speed,
                        "eta_seconds": eta_seconds,
                        "logs": logs_list[-40:],
                    })

                    batch_counter += 1
                    if batch_counter >= 25:
                        batch_counter = 0
                        rest_time = random.uniform(3.5, 6.0)
                        logs_list.append(f"[{cur_time()}] Pause ({rest_time:.1f}s) to maintain rate limit safety...")
                        await cls.broadcast(task_uuid, {
                            "status": "RUNNING",
                            "logs": logs_list[-40:],
                            "total": total_members,
                            "success": success,
                            "already": already_in,
                            "already_in": already_in,
                            "failed": failed,
                            "rate_limits": rate_limited,
                            "rate_limited": rate_limited,
                        })
                        await asyncio.sleep(rest_time)
                    else:
                        base_delay = max(task.delay_ms / 1000.0, 1.2)
                        jitter = random.uniform(0.1, 0.4)
                        await asyncio.sleep(base_delay + jitter)

                logs_list.append(f"[{cur_time()}] Restore finished. {success} added, {already_in} already in server, {failed} failed.")

                async with async_session_factory() as db:
                    await db.execute(
                        update(PullTask)
                        .where(PullTask.task_uuid == task_uuid)
                        .values(
                            status="COMPLETED",
                            success_count=success,
                            failed_count=failed,
                            already_in_guild_count=already_in,
                            rate_limited_count=rate_limited,
                            logs=json.dumps(logs_list[-100:]),
                            updated_at=datetime.utcnow(),
                        )
                    )
                    await db.commit()

                await cls.broadcast(task_uuid, {
                    "status": "COMPLETED",
                    "progress": 100,
                    "processed": total_members,
                    "total": total_members,
                    "success": success,
                    "already": already_in,
                    "already_in": already_in,
                    "failed": failed,
                    "rate_limits": rate_limited,
                    "rate_limited": rate_limited,
                    "logs": logs_list[-40:],
                    "message": "Migration completed successfully.",
                })

                logger.info(f"Migration task {task_uuid} completed. Success: {success}, Already in: {already_in}, Failed: {failed}")

        except Exception as e:
            logger.exception(f"Unhandled migration worker error for task {task_uuid}: {e}")
            err_msg = f"Migration worker error: {str(e)}"
            logs_list.append(f"[{cur_time()}] {err_msg}")
            try:
                async with async_session_factory() as db:
                    await db.execute(
                        update(PullTask)
                        .where(PullTask.task_uuid == task_uuid)
                        .values(
                            status="FAILED",
                            error_message=str(e),
                            logs=json.dumps(logs_list[-100:]),
                            updated_at=datetime.utcnow(),
                        )
                    )
                    await db.commit()
            except Exception as db_err:
                logger.error(f"Failed to record task failure to DB for {task_uuid}: {db_err}")

            await cls.broadcast(task_uuid, {
                "status": "FAILED",
                "progress": 0,
                "logs": logs_list[-40:],
                "error": str(e),
                "message": err_msg,
            })
        finally:
            if task_uuid in _engine_tasks:
                _engine_tasks[task_uuid]["running"] = False

    @classmethod
    async def schedule_worker(cls):
        while True:
            try:
                await asyncio.sleep(10)
                async with async_session_factory() as db:
                    now_utc = datetime.utcnow()
                    stmt = select(PullTask).where(
                        (PullTask.status == "SCHEDULED") & (PullTask.scheduled_for <= now_utc)
                    )
                    res = await db.execute(stmt)
                    due_tasks = res.scalars().all()
                    for t in due_tasks:
                        t.status = "PENDING"
                        await db.commit()
                        asyncio.create_task(cls.start(t.task_uuid, min_stay_days=t.min_stay_days))
            except Exception as e:
                logger.debug(f"Schedule worker check error: {e}")

