import io
import json
import uuid
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Set
import discord
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Backup, Bot
from services.webhook_service import WebhookService

logger = logging.getLogger("freecord.backup_service")

_restore_states: Dict[str, Dict[str, Any]] = {}
_restore_ws: Dict[str, Set[Any]] = {}
_restore_stops: Dict[str, asyncio.Event] = {}


class BackupService:
    @staticmethod
    def register_ws(restore_id: str, ws: Any):
        if restore_id not in _restore_ws:
            _restore_ws[restore_id] = set()
        _restore_ws[restore_id].add(ws)

    @staticmethod
    def unregister_ws(restore_id: str, ws: Any):
        if restore_id in _restore_ws:
            _restore_ws[restore_id].discard(ws)
            if not _restore_ws[restore_id]:
                del _restore_ws[restore_id]

    @staticmethod
    def get_restore_state(restore_id: str) -> Optional[Dict[str, Any]]:
        return _restore_states.get(restore_id)

    @classmethod
    def stop_restore(cls, restore_id: str) -> bool:
        if restore_id in _restore_stops:
            _restore_stops[restore_id].set()
            return True
        return False

    @staticmethod
    async def broadcast_restore(restore_id: str, payload: Dict[str, Any]):
        _restore_states[restore_id] = payload
        subs = _restore_ws.get(restore_id, set())
        dead = []
        for ws in list(subs):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for d in dead:
            subs.discard(d)

    @staticmethod
    async def create_guild_backup(
        guild: discord.Guild,
        bot_db_id: int,
        db: AsyncSession,
        include_messages: bool = True,
        max_messages_per_channel: int = 50,
    ) -> Backup:
        data: Dict[str, Any] = {
            "guild_id": str(guild.id),
            "name": guild.name,
            "description": guild.description,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "banner_url": str(guild.banner.url) if guild.banner else None,
            "splash_url": str(guild.splash.url) if guild.splash else None,
            "afk_timeout": guild.afk_timeout,
            "afk_channel": guild.afk_channel.name if guild.afk_channel else None,
            "system_channel": guild.system_channel.name if guild.system_channel else None,
            "rules_channel": guild.rules_channel.name if guild.rules_channel else None,
            "verification_level": str(guild.verification_level),
            "default_notifications": str(guild.default_notifications),
            "explicit_content_filter": str(guild.explicit_content_filter),
            "preferred_locale": str(guild.preferred_locale),
            "created_at": datetime.utcnow().isoformat(),
        }

        roles = []
        for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
            if r.is_default():
                roles.append({
                    "id": str(r.id),
                    "name": "@everyone",
                    "color": r.color.value,
                    "hoist": r.hoist,
                    "position": r.position,
                    "permissions": r.permissions.value,
                    "mentionable": r.mentionable,
                    "is_default": True,
                })
            elif not r.managed:
                roles.append({
                    "id": str(r.id),
                    "name": r.name,
                    "color": r.color.value,
                    "hoist": r.hoist,
                    "position": r.position,
                    "permissions": r.permissions.value,
                    "mentionable": r.mentionable,
                    "is_default": False,
                })
        data["roles"] = roles

        categories = []
        for cat in sorted(guild.categories, key=lambda c: c.position):
            overwrites = []
            for target, ow in cat.overwrites.items():
                if isinstance(target, discord.Role):
                    overwrites.append({
                        "role_name": target.name,
                        "allow": ow.pair()[0].value,
                        "deny": ow.pair()[1].value,
                    })
            categories.append({
                "id": str(cat.id),
                "name": cat.name,
                "position": cat.position,
                "overwrites": overwrites,
            })
        data["categories"] = categories

        channels = []
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue

            overwrites = []
            for target, ow in ch.overwrites.items():
                if isinstance(target, discord.Role):
                    overwrites.append({
                        "role_name": target.name,
                        "allow": ow.pair()[0].value,
                        "deny": ow.pair()[1].value,
                    })

            ch_item: Dict[str, Any] = {
                "id": str(ch.id),
                "name": ch.name,
                "position": ch.position,
                "category_name": ch.category.name if ch.category else None,
                "overwrites": overwrites,
            }

            if isinstance(ch, discord.TextChannel):
                ch_item["type"] = "text"
                ch_item["topic"] = ch.topic
                ch_item["nsfw"] = ch.nsfw
                ch_item["slowmode_delay"] = ch.slowmode_delay

                if include_messages:
                    msgs = []
                    try:
                        async for m in ch.history(limit=max_messages_per_channel):
                            msgs.append({
                                "author": str(m.author),
                                "content": m.content,
                                "created_at": m.created_at.isoformat(),
                                "attachments": [a.url for a in m.attachments],
                                "embeds": [e.to_dict() for e in m.embeds],
                            })
                        ch_item["messages"] = list(reversed(msgs))
                    except Exception:
                        ch_item["messages"] = []

            elif isinstance(ch, discord.VoiceChannel):
                ch_item["type"] = "voice"
                ch_item["bitrate"] = ch.bitrate
                ch_item["user_limit"] = ch.user_limit
            elif isinstance(ch, discord.StageChannel):
                ch_item["type"] = "stage"
                ch_item["bitrate"] = ch.bitrate
                ch_item["user_limit"] = ch.user_limit
            elif isinstance(ch, discord.ForumChannel):
                ch_item["type"] = "forum"
                ch_item["topic"] = ch.topic
                ch_item["nsfw"] = ch.nsfw
            else:
                ch_item["type"] = "unknown"

            channels.append(ch_item)

        data["channels"] = channels

        emojis = []
        for em in guild.emojis:
            emojis.append({
                "id": str(em.id),
                "name": em.name,
                "url": str(em.url),
                "animated": em.animated,
            })
        data["emojis"] = emojis

        stickers = []
        for st in guild.stickers:
            stickers.append({
                "id": str(st.id),
                "name": st.name,
                "description": st.description,
                "url": str(st.url),
            })
        data["stickers"] = stickers

        json_str = json.dumps(data, indent=2)
        task_id = f"fc_{uuid.uuid4().hex[:12]}"

        record = Backup(
            backup_uuid=task_id,
            guild_id=str(guild.id),
            bot_id=bot_db_id,
            guild_name=guild.name,
            icon_url=data["icon_url"],
            data_json=json_str,
            roles_count=len(roles),
            channels_count=len(channels),
            emojis_count=len(emojis),
            stickers_count=len(stickers),
            size_bytes=len(json_str.encode("utf-8")),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def restore_guild_backup(
        guild: discord.Guild,
        backup_data: Dict[str, Any],
        restore_roles: bool = True,
        restore_channels: bool = True,
        restore_emojis: bool = True,
        wipe_first: bool = False,
    ) -> Dict[str, Any]:
        res = {"roles": 0, "categories": 0, "channels": 0, "emojis": 0, "errors": []}

        try:
            g_name = backup_data.get("name")
            if g_name and wipe_first:
                await guild.edit(name=g_name)
        except Exception as e:
            res["errors"].append(str(e))

        if wipe_first:
            for ch in list(guild.channels):
                try:
                    await ch.delete()
                except Exception:
                    pass

            for r in list(guild.roles):
                if not r.is_default() and not r.managed and r < guild.me.top_role:
                    try:
                        await r.delete()
                    except Exception:
                        pass

            for em in list(guild.emojis):
                try:
                    await em.delete(reason="FreeCord Server Wipe")
                except Exception:
                    pass

            for st in list(guild.stickers):
                try:
                    await st.delete(reason="FreeCord Server Wipe")
                except Exception:
                    pass

        role_map: Dict[str, discord.Role] = {"@everyone": guild.default_role}

        if restore_roles:
            for r_info in sorted(backup_data.get("roles", []), key=lambda x: x.get("position", 0)):
                if r_info.get("is_default"):
                    try:
                        perms = discord.Permissions(r_info.get("permissions", 0))
                        await guild.default_role.edit(permissions=perms)
                    except Exception as e:
                        res["errors"].append(str(e))
                else:
                    try:
                        color = discord.Color(r_info.get("color", 0))
                        perms = discord.Permissions(r_info.get("permissions", 0))
                        new_r = await guild.create_role(
                            name=r_info["name"],
                            color=color,
                            hoist=r_info.get("hoist", False),
                            mentionable=r_info.get("mentionable", False),
                            permissions=perms,
                        )
                        role_map[new_r.name] = new_r
                        res["roles"] += 1
                    except Exception as e:
                        res["errors"].append(str(e))

        cat_map: Dict[str, discord.CategoryChannel] = {}
        if restore_channels:
            for cat_info in backup_data.get("categories", []):
                ow_map = {}
                for ow in cat_info.get("overwrites", []):
                    target = role_map.get(ow["role_name"])
                    if target:
                        allow = discord.Permissions(ow.get("allow", 0))
                        deny = discord.Permissions(ow.get("deny", 0))
                        ow_map[target] = discord.PermissionOverwrite.from_pair(allow, deny)
                try:
                    new_cat = await guild.create_category(
                        name=cat_info["name"],
                        position=cat_info.get("position", 0),
                        overwrites=ow_map,
                    )
                    cat_map[new_cat.name] = new_cat
                    res["categories"] += 1
                except Exception as e:
                    res["errors"].append(str(e))

            for ch_info in backup_data.get("channels", []):
                cat = cat_map.get(ch_info.get("category_name", ""))
                ow_map = {}
                for ow in ch_info.get("overwrites", []):
                    target = role_map.get(ow["role_name"])
                    if target:
                        allow = discord.Permissions(ow.get("allow", 0))
                        deny = discord.Permissions(ow.get("deny", 0))
                        ow_map[target] = discord.PermissionOverwrite.from_pair(allow, deny)

                ch_type = ch_info.get("type", "text")
                try:
                    if ch_type == "text":
                        await guild.create_text_channel(
                            name=ch_info["name"],
                            category=cat,
                            topic=ch_info.get("topic"),
                            slowmode_delay=ch_info.get("slowmode_delay", 0),
                            nsfw=ch_info.get("nsfw", False),
                            overwrites=ow_map,
                        )
                        res["channels"] += 1
                    elif ch_type == "voice":
                        await guild.create_voice_channel(
                            name=ch_info["name"],
                            category=cat,
                            user_limit=ch_info.get("user_limit", 0),
                            overwrites=ow_map,
                        )
                        res["channels"] += 1
                    elif ch_type == "stage":
                        await guild.create_stage_channel(
                            name=ch_info["name"],
                            category=cat,
                            overwrites=ow_map,
                        )
                        res["channels"] += 1
                except Exception as e:
                    res["errors"].append(str(e))

        if restore_emojis:
            for em_info in backup_data.get("emojis", []):
                em_url = em_info.get("url")
                em_name = em_info.get("name")
                if em_url and em_name:
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as http_client:
                            resp = await http_client.get(em_url)
                            if resp.status_code == 200:
                                await guild.create_custom_emoji(name=em_name, image=resp.content, reason="FreeCord Backup Restore")
                                res["emojis"] += 1
                    except Exception as e:
                        res["errors"].append(str(e))

        return res

    @classmethod
    async def restore_guild_backup_live(
        cls,
        restore_id: str,
        guild: discord.Guild,
        backup_data: Dict[str, Any],
        restore_roles: bool = True,
        restore_channels: bool = True,
        restore_emojis: bool = True,
        wipe_first: bool = False,
    ):
        stop_event = asyncio.Event()
        _restore_stops[restore_id] = stop_event
        logs_list = []
        def cur_time():
            return datetime.now().strftime("%H:%M:%S")

        async def emit(pct: int, status: str = "RUNNING", roles: int = 0, cats: int = 0, chs: int = 0, ems: int = 0):
            await cls.broadcast_restore(restore_id, {
                "restore_id": restore_id,
                "status": status,
                "progress": pct,
                "roles": roles,
                "categories": cats,
                "channels": chs,
                "emojis": ems,
                "logs": logs_list,
            })

        roles_list = backup_data.get("roles", []) if restore_roles else []
        cats_list = backup_data.get("categories", []) if restore_channels else []
        chs_list = backup_data.get("channels", []) if restore_channels else []
        emojis_list = backup_data.get("emojis", []) if restore_emojis else []

        total_items = max(1, (len(roles_list) if restore_roles else 0) + (len(cats_list) + len(chs_list) if restore_channels else 0) + (len(emojis_list) if restore_emojis else 0) + 1)
        processed_items = 0

        logs_list.append(f"[{cur_time()}] Starting restore into '{guild.name}' ({guild.id})...")
        await emit(5, "RUNNING")

        roles_done = 0
        cats_done = 0
        chs_done = 0
        emojis_done = 0

        try:
            if stop_event.is_set():
                logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                await emit(0, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                return

            g_name = backup_data.get("name")
            if g_name and wipe_first:
                try:
                    await guild.edit(name=g_name)
                    logs_list.append(f"[{cur_time()}] Renamed server to '{g_name}'")
                except Exception as e:
                    logs_list.append(f"[{cur_time()}] Warning: Could not rename server: {e}")

            if wipe_first:
                logs_list.append(f"[{cur_time()}] Cleaning existing channels, custom roles, emojis, and stickers...")
                await emit(10, "RUNNING")
                for ch in list(guild.channels):
                    if stop_event.is_set():
                        logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                        await emit(10, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        return
                    try:
                        await ch.delete()
                    except Exception:
                        pass
                for r in list(guild.roles):
                    if stop_event.is_set():
                        logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                        await emit(10, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        return
                    if not r.is_default() and not r.managed and r < guild.me.top_role:
                        try:
                            await r.delete()
                        except Exception:
                            pass
                for em in list(guild.emojis):
                    if stop_event.is_set():
                        logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                        await emit(10, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        return
                    try:
                        await em.delete(reason="FreeCord Server Wipe")
                    except Exception:
                        pass
                for st in list(guild.stickers):
                    if stop_event.is_set():
                        logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                        await emit(10, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        return
                    try:
                        await st.delete(reason="FreeCord Server Wipe")
                    except Exception:
                        pass
                logs_list.append(f"[{cur_time()}] Target server cleared.")

            role_map: Dict[str, discord.Role] = {"@everyone": guild.default_role}

            if restore_roles and roles_list:
                logs_list.append(f"[{cur_time()}] Restoring {len(roles_list)} server roles...")
                for r_info in sorted(roles_list, key=lambda x: x.get("position", 0)):
                    if stop_event.is_set():
                        logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                        pct = min(85, 10 + int((processed_items / total_items) * 75))
                        await emit(pct, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        return

                    if r_info.get("is_default"):
                        try:
                            perms = discord.Permissions(int(r_info.get("permissions", 0) or 0))
                            await guild.default_role.edit(permissions=perms)
                        except Exception:
                            pass
                    else:
                        try:
                            color = discord.Color(int(r_info.get("color", 0) or 0))
                            perms = discord.Permissions(int(r_info.get("permissions", 0) or 0))
                            new_r = await guild.create_role(
                                name=r_info["name"],
                                color=color,
                                hoist=r_info.get("hoist", False),
                                mentionable=r_info.get("mentionable", False),
                                permissions=perms,
                            )
                            role_map[new_r.name] = new_r
                            roles_done += 1
                            logs_list.append(f"[{cur_time()}] Created role: @{new_r.name}")
                        except Exception as e:
                            logs_list.append(f"[{cur_time()}] Skipped role '{r_info.get('name')}': {e}")
                    processed_items += 1
                    pct = min(85, 10 + int((processed_items / total_items) * 75))
                    await emit(pct, "RUNNING", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                    await asyncio.sleep(0.08)

            cat_map: Dict[str, discord.CategoryChannel] = {}
            if restore_channels:
                if cats_list:
                    logs_list.append(f"[{cur_time()}] Restoring {len(cats_list)} categories...")
                    for cat_info in cats_list:
                        if stop_event.is_set():
                            logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                            pct = min(85, 10 + int((processed_items / total_items) * 75))
                            await emit(pct, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                            return

                        ow_map = {}
                        for ow in cat_info.get("overwrites", []):
                            target = role_map.get(ow["role_name"])
                            if target:
                                allow = discord.Permissions(int(ow.get("allow", 0) or 0))
                                deny = discord.Permissions(int(ow.get("deny", 0) or 0))
                                ow_map[target] = discord.PermissionOverwrite.from_pair(allow, deny)
                        try:
                            new_cat = await guild.create_category(
                                name=cat_info["name"],
                                position=cat_info.get("position", 0),
                                overwrites=ow_map,
                            )
                            cat_map[new_cat.name] = new_cat
                            cats_done += 1
                            logs_list.append(f"[{cur_time()}] Created category: {new_cat.name}")
                        except Exception as e:
                            logs_list.append(f"[{cur_time()}] Skipped category '{cat_info.get('name')}': {e}")
                        processed_items += 1
                        pct = min(85, 10 + int((processed_items / total_items) * 75))
                        await emit(pct, "RUNNING", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        await asyncio.sleep(0.08)

                if chs_list:
                    logs_list.append(f"[{cur_time()}] Restoring {len(chs_list)} channels...")
                    for ch_info in chs_list:
                        if stop_event.is_set():
                            logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                            pct = min(92, 10 + int((processed_items / total_items) * 75))
                            await emit(pct, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                            return

                        cat = cat_map.get(ch_info.get("category_name", ""))
                        ow_map = {}
                        for ow in ch_info.get("overwrites", []):
                            target = role_map.get(ow["role_name"])
                            if target:
                                allow = discord.Permissions(int(ow.get("allow", 0) or 0))
                                deny = discord.Permissions(int(ow.get("deny", 0) or 0))
                                ow_map[target] = discord.PermissionOverwrite.from_pair(allow, deny)

                        ch_type = ch_info.get("type", "text")
                        try:
                            if ch_type == "text":
                                await guild.create_text_channel(
                                    name=ch_info["name"],
                                    category=cat,
                                    topic=ch_info.get("topic"),
                                    slowmode_delay=ch_info.get("slowmode_delay", 0),
                                    nsfw=ch_info.get("nsfw", False),
                                    overwrites=ow_map,
                                )
                                chs_done += 1
                                logs_list.append(f"[{cur_time()}] Created channel: #{ch_info['name']}")
                            elif ch_type == "voice":
                                await guild.create_voice_channel(
                                    name=ch_info["name"],
                                    category=cat,
                                    user_limit=ch_info.get("user_limit", 0),
                                    overwrites=ow_map,
                                )
                                chs_done += 1
                                logs_list.append(f"[{cur_time()}] Created voice channel: {ch_info['name']}")
                            elif ch_type == "stage":
                                await guild.create_stage_channel(
                                    name=ch_info["name"],
                                    category=cat,
                                    overwrites=ow_map,
                                )
                                chs_done += 1
                                logs_list.append(f"[{cur_time()}] Created stage channel: {ch_info['name']}")
                        except Exception as e:
                            logs_list.append(f"[{cur_time()}] Skipped channel '{ch_info.get('name')}': {e}")
                        processed_items += 1
                        pct = min(92, 10 + int((processed_items / total_items) * 75))
                        await emit(pct, "RUNNING", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        await asyncio.sleep(0.08)

            if restore_emojis and emojis_list:
                logs_list.append(f"[{cur_time()}] Restoring {len(emojis_list)} custom emojis...")
                for em_info in emojis_list:
                    if stop_event.is_set():
                        logs_list.append(f"[{cur_time()}] Restore cancelled by user.")
                        pct = min(98, 10 + int((processed_items / total_items) * 75))
                        await emit(pct, "STOPPED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                        return

                    em_url = em_info.get("url")
                    em_name = em_info.get("name")
                    if em_url and em_name:
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as http_client:
                                resp = await http_client.get(em_url)
                                if resp.status_code == 200:
                                    await guild.create_custom_emoji(name=em_name, image=resp.content, reason="FreeCord Backup Restore")
                                    emojis_done += 1
                                    logs_list.append(f"[{cur_time()}] Created emoji: :{em_name}:")
                        except Exception as e:
                            logs_list.append(f"[{cur_time()}] Skipped emoji ':{em_name}:': {e}")
                    processed_items += 1
                    pct = min(98, 10 + int((processed_items / total_items) * 75))
                    await emit(pct, "RUNNING", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
                    await asyncio.sleep(0.08)

            logs_list.append(f"[{cur_time()}] Restore completed. Restored {roles_done} roles, {cats_done} categories, {chs_done} channels, {emojis_done} emojis.")
            await emit(100, "COMPLETED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)

        except Exception as err:
            logs_list.append(f"[{cur_time()}] Restore error: {err}")
            await emit(100, "FAILED", roles=roles_done, cats=cats_done, chs=chs_done, ems=emojis_done)
        finally:
            _restore_stops.pop(restore_id, None)

