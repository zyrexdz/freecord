import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import httpx

logger = logging.getLogger("freecord.webhooks")

class WebhookService:
    @staticmethod
    async def send_embed(
        webhook_url: str,
        title: str,
        description: str,
        color: int = 0x5865F2,
        fields: Optional[List[Dict[str, Any]]] = None,
        footer: str = "FreeCord Platform",
        thumbnail_url: Optional[str] = None,
        author: Optional[Dict[str, str]] = None,
    ) -> bool:
        if not webhook_url:
            return False

        embed: Dict[str, Any] = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": footer, "icon_url": "https://cdn.discordapp.com/embed/avatars/0.png"},
        }
        if fields:
            embed["fields"] = fields
        if thumbnail_url:
            embed["thumbnail"] = {"url": thumbnail_url}
        if author:
            embed["author"] = author

        payload = {
            "username": "FreeCord",
            "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
            "embeds": [embed],
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(webhook_url, json=payload)
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to dispatch webhook: {e}")
            return False

    @classmethod
    async def log_verification_success(
        cls,
        webhook_url: Optional[str],
        guild_name: str,
        user_id: str,
        username: str,
        avatar_url: Optional[str],
        ip: str,
        country: str,
        isp: str,
        role_name: Optional[str] = None,
    ):
        if not webhook_url:
            return

        fields = [
            {"name": "User", "value": f"<@{user_id}> (`{username}`)", "inline": True},
            {"name": "User ID", "value": f"`{user_id}`", "inline": True},
            {"name": "Location", "value": f":flag_{country.lower() if len(country)==2 else 'white'}: {country} ({isp})", "inline": True},
            {"name": "IP Address", "value": f"||`{ip}`||", "inline": True},
        ]
        if role_name:
            fields.append({"name": "Role Granted", "value": f"`@{role_name}`", "inline": True})

        await cls.send_embed(
            webhook_url=webhook_url,
            title="🛡️ Member Verified Successfully",
            description=f"A member has verified into **{guild_name}**.",
            color=0x57F287,
            fields=fields,
            thumbnail_url=avatar_url,
        )

    @classmethod
    async def log_verification_failed(
        cls,
        webhook_url: Optional[str],
        guild_name: str,
        user_id: str,
        username: str,
        reason: str,
        ip: str,
        isp: str,
        country: str,
    ):
        if not webhook_url:
            return

        fields = [
            {"name": "User", "value": f"<@{user_id}> (`{username}`)", "inline": True},
            {"name": "Reason", "value": f"❌ **{reason}**", "inline": True},
            {"name": "Location / ISP", "value": f"{country} ({isp})", "inline": True},
            {"name": "Flagged IP", "value": f"||`{ip}`||", "inline": True},
        ]

        await cls.send_embed(
            webhook_url=webhook_url,
            title="🚨 Verification Blocked by Firewall",
            description=f"A member was blocked from verifying into **{guild_name}**.",
            color=0xED4245,
            fields=fields,
        )

    @classmethod
    async def log_migration_milestone(
        cls,
        webhook_url: Optional[str],
        guild_name: str,
        percentage: int,
        success: int,
        failed: int,
        total: int,
        status: str,
    ):
        if not webhook_url:
            return

        color = 0x5865F2
        if status == "COMPLETED":
            color = 0x57F287
        elif status == "FAILED":
            color = 0xED4245

        fields = [
            {"name": "Progress", "value": f"**{percentage}%** (`{success + failed}/{total}`)", "inline": True},
            {"name": "Successful", "value": f"✅ `{success}`", "inline": True},
            {"name": "Failed", "value": f"❌ `{failed}`", "inline": True},
            {"name": "Status", "value": f"`{status}`", "inline": True},
        ]

        await cls.send_embed(
            webhook_url=webhook_url,
            title=f"🚀 Migration Alert: {percentage}% Reached",
            description=f"Member pull task for **{guild_name}** status update.",
            color=color,
            fields=fields,
        )

    @classmethod
    async def log_backup_event(
        cls,
        webhook_url: Optional[str],
        guild_name: str,
        event_name: str,
        backup_uuid: str,
        details: str,
    ):
        if not webhook_url:
            return

        await cls.send_embed(
            webhook_url=webhook_url,
            title=f"💾 Server Backup: {event_name}",
            description=f"Backup event for **{guild_name}**.\n\n{details}\n**Backup ID**: `{backup_uuid}`",
            color=0xFEE75C,
        )
