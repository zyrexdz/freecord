import json
import logging
from typing import Tuple, Dict, Any, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import check_ip, verify_turnstile, verify_hcaptcha, account_age_days
from database.models import GuildConfig, Blacklist, AuditLog

logger = logging.getLogger("freecord.security_service")


class SecurityService:
    @staticmethod
    async def evaluate_member_security(
        db: AsyncSession,
        guild_config: GuildConfig,
        user_id: str,
        client_ip: str,
        captcha_token: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        telemetry: Dict[str, Any] = {
            "ip": client_ip,
            "is_vpn": False,
            "is_proxy": False,
            "is_datacenter": False,
            "is_cellular": False,
            "country": "Unknown",
            "country_code": "XX",
            "city": "Unknown",
            "isp": "Unknown",
            "asn": "Unknown",
            "account_age_days": round(account_age_days(user_id), 1),
        }

        user_bl = await db.execute(
            select(Blacklist).where(
                (Blacklist.type == "USER_ID") & 
                (Blacklist.value == str(user_id)) &
                ((Blacklist.guild_id == None) | (Blacklist.guild_id == "ALL") | (Blacklist.guild_id == guild_config.guild_id))
            )
        )
        if user_bl.scalars().first():
            return False, "Your Discord account is blacklisted from this server.", telemetry

        ip_bl = await db.execute(
            select(Blacklist).where(
                (Blacklist.type == "IP") & 
                (Blacklist.value == str(client_ip)) &
                ((Blacklist.guild_id == None) | (Blacklist.guild_id == "ALL") | (Blacklist.guild_id == guild_config.guild_id))
            )
        )
        if ip_bl.scalars().first():
            return False, "Your IP address is blacklisted from this server.", telemetry

        if guild_config.captcha_enabled:
            if not captcha_token:
                return False, "CAPTCHA verification is required.", telemetry

            if guild_config.captcha_provider == "hcaptcha":
                valid, msg = await verify_hcaptcha(captcha_token, remote_ip=client_ip)
            else:
                valid, msg = await verify_turnstile(captcha_token, remote_ip=client_ip)

            if not valid:
                return False, f"CAPTCHA failed: {msg}", telemetry

        if guild_config.min_account_age_days > 0:
            age_days = telemetry["account_age_days"]
            if age_days < guild_config.min_account_age_days:
                return False, f"Account too new ({age_days:.0f} days). Minimum required: {guild_config.min_account_age_days} days.", telemetry

        if guild_config.firewall_enabled:
            ip_info = await check_ip(client_ip)
            telemetry.update(ip_info)

            if telemetry.get("asn"):
                asn_bl = await db.execute(
                    select(Blacklist).where(
                        (Blacklist.type == "ASN") & 
                        (Blacklist.value == str(telemetry["asn"])) &
                        ((Blacklist.guild_id == None) | (Blacklist.guild_id == "ALL") | (Blacklist.guild_id == guild_config.guild_id))
                    )
                )
                if asn_bl.scalars().first():
                    return False, f"Your network provider ({telemetry['asn']}) is blacklisted.", telemetry

            if guild_config.anti_vpn_enabled and (telemetry["is_vpn"] or telemetry["is_proxy"]):
                return False, "VPN or Proxy connection detected. Please disable it to verify.", telemetry

            if guild_config.block_datacenter and telemetry["is_datacenter"]:
                return False, "Datacenter IP detected. Please use a residential internet connection.", telemetry

            if guild_config.block_cellular and telemetry["is_cellular"]:
                return False, "Cellular (LTE/5G) connections are restricted. Please use Wi-Fi or broadband.", telemetry

        return True, "Passed security firewall verification.", telemetry

    @staticmethod
    async def record_audit_log(
        db: AsyncSession,
        event_type: str,
        description: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        try:
            entry = AuditLog(
                event_type=event_type,
                guild_id=guild_id,
                user_id=user_id,
                description=description,
                ip_address=ip_address,
                metadata_json=json.dumps(metadata or {}),
            )
            db.add(entry)
            await db.commit()
        except Exception as e:
            logger.error(f"Failed to record audit log: {e}")

