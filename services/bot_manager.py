import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, update

from core.config import get_settings, detect_network_addresses
from core.security import decrypt_secret
from database.session import async_session_factory
from database.models import Bot, GuildConfig, Backup, PullTask, Blacklist, MemberToken
from services.backup_service import BackupService
from services.webhook_service import WebhookService

logger = logging.getLogger("freecord.bot_manager")


class FreeCordBotClient(commands.Bot):
    _synced_bot_ids = set()

    def __init__(self, bot_db_id: int, client_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot_db_id = bot_db_id
        self.client_id = client_id
        self.is_synced = False

    async def setup_hook(self):
        self._register_slash_commands()
        if self.bot_db_id not in FreeCordBotClient._synced_bot_ids:
            asyncio.create_task(self._sync_commands_safely())

    async def _sync_commands_safely(self):
        try:
            await asyncio.sleep(2)
            synced = await self.tree.sync()
            FreeCordBotClient._synced_bot_ids.add(self.bot_db_id)
            logger.info(f"Synced {len(synced)} slash commands for Bot ID {self.bot_db_id}")
            self.is_synced = True
        except discord.errors.HTTPException as he:
            if he.status == 429:
                logger.debug(f"Command sync rate-limited for Bot {self.bot_db_id}, will retry later.")
            else:
                logger.warning(f"Error syncing slash commands for Bot {self.bot_db_id}: {he}")
        except Exception as e:
            logger.warning(f"Error syncing slash commands for Bot {self.bot_db_id}: {e}")

    def _register_slash_commands(self):
        bot_db_id = self.bot_db_id

        def _is_guild_admin(interaction: discord.Interaction) -> bool:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                return False
            return interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id

        @self.tree.command(name="backup_create", description="Create a full backup of this Discord server.")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def backup_create(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            try:
                async with async_session_factory() as db:
                    backup = await BackupService.create_guild_backup(
                        guild=interaction.guild,
                        bot_db_id=bot_db_id,
                        db=db,
                        include_messages=True,
                    )
                
                net_info = detect_network_addresses()
                preview_url = f"{net_info['recommended_base_url']}/backups"
                
                embed = discord.Embed(
                    title="💾 FreeCord Server Backup Created",
                    description=f"Successfully backed up **{interaction.guild.name}**!\n\n"
                                f"• **Backup ID**: `{backup.backup_uuid}`\n"
                                f"• **Roles**: `{backup.roles_count}`\n"
                                f"• **Channels**: `{backup.channels_count}`\n"
                                f"• **Emojis & Stickers**: `{backup.emojis_count + backup.stickers_count}`\n\n"
                                f"[View & Manage in Web Dashboard]({preview_url})",
                    color=0x57F287,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Slash command backup_create failed: {e}")
                await interaction.followup.send(f"❌ Backup failed: {str(e)}", ephemeral=True)

        @self.tree.command(name="backup_restore", description="Restore a FreeCord backup into this server.")
        @app_commands.describe(
            backup_id="The UUID of the backup (e.g. fc_1234567890ab)",
            wipe_first="Wipe existing channels, roles, and emojis before restoring (default: False)",
        )
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def backup_restore(interaction: discord.Interaction, backup_id: str, wipe_first: Optional[bool] = False):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            try:
                async with async_session_factory() as db:
                    stmt = select(Backup).where(Backup.backup_uuid == backup_id)
                    res = await db.execute(stmt)
                    backup = res.scalars().first()

                    if not backup:
                        await interaction.followup.send(f"❌ Backup `{backup_id}` not found.", ephemeral=True)
                        return

                    backup_data = json.loads(backup.data_json)
                    results = await BackupService.restore_guild_backup(
                        guild=interaction.guild,
                        backup_data=backup_data,
                        restore_roles=True,
                        restore_channels=True,
                        restore_emojis=True,
                        wipe_first=bool(wipe_first),
                    )

                embed = discord.Embed(
                    title="🔄 Backup Restoration Completed",
                    description=f"Restored backup `{backup_id}` into **{interaction.guild.name}**.\n\n"
                                f"• **Roles Created**: `{results['roles']}`\n"
                                f"• **Categories Created**: `{results['categories']}`\n"
                                f"• **Channels Created**: `{results['channels']}`\n"
                                f"• **Emojis Created**: `{results['emojis']}`",
                    color=0x5865F2,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                logger.error(f"Slash command backup_restore failed: {e}")
                await interaction.followup.send(f"❌ Restoration failed: {str(e)}", ephemeral=True)

        @self.tree.command(name="pull_start", description="Initiate a member restore into this server.")
        @app_commands.describe(source_guild_id="Optional: Source guild ID to filter members from")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def pull_start(interaction: discord.Interaction, source_guild_id: Optional[str] = None):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            import uuid
            task_uuid = f"pull_{uuid.uuid4().hex[:10]}"
            target_guild_id = str(interaction.guild.id)

            from services.migration_service import MigrationService

            async with async_session_factory() as db:
                stmt = select(MemberToken).where(
                    (MemberToken.bot_id == bot_db_id) & (MemberToken.is_blacklisted == False)
                )
                if source_guild_id:
                    stmt = stmt.where(MemberToken.source_guild_id == source_guild_id)
                res = await db.execute(stmt)
                members_count = len(res.scalars().all())

                task = PullTask(
                    task_uuid=task_uuid,
                    bot_id=bot_db_id,
                    source_guild_id=source_guild_id,
                    target_guild_id=target_guild_id,
                    total_members=members_count,
                    status="PENDING",
                    delay_ms=1000,
                )
                db.add(task)
                await db.commit()

            await MigrationService.start_pull_task(task_uuid)

            net_info = detect_network_addresses()
            dashboard_url = f"{net_info['recommended_base_url']}/migrations"

            embed = discord.Embed(
                title="🚀 Member Restore Started",
                description=f"Restore task launched for **{interaction.guild.name}**.\n\n"
                            f"• **Task ID**: `{task_uuid}`\n"
                            f"• **Total Verified Members**: `{members_count}`\n\n"
                            f"[Monitor Live Progress on Web Dashboard]({dashboard_url})",
                color=0x5865F2,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="pull_status", description="Check status of recent member restore tasks.")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def pull_status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            async with async_session_factory() as db:
                stmt = select(PullTask).where(PullTask.bot_id == bot_db_id).order_by(PullTask.created_at.desc()).limit(3)
                res = await db.execute(stmt)
                tasks = res.scalars().all()

            if not tasks:
                await interaction.followup.send("ℹ️ No restore tasks recorded for this bot.", ephemeral=True)
                return

            embed = discord.Embed(title="📊 Member Restore Status", color=0x5865F2)
            for t in tasks:
                embed.add_field(
                    name=f"Task `{t.task_uuid}` - {t.status}",
                    value=f"Target: `{t.target_guild_id}` | Added: `{t.success_count}/{t.total_members}` | Failed: `{t.failed_count}`",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="credits", description="Show FreeCord credits and open source project details.")
        async def credits(interaction: discord.Interaction):
            embed = discord.Embed(
                title="FreeCord Project & Credits",
                description=(
                    "FreeCord is an open source Discord backup, security, and member restore platform.\n\n"
                    "**Created by:** ZyreXDZ\n"
                    "**GitHub:** https://github.com/zyrexdz/freecord\n\n"
                    "Built for server owners to have complete control over their community."
                ),
                color=0x5865F2,
            )
            embed.set_footer(text="FreeCord | Open Source")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="verify_setup", description="Configure verification for this server.")
        @app_commands.describe(verified_role="Optional: Role for verified members (defaults to auto-created Verified role)", log_channel="Optional: Channel for verification logs")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def verify_setup(
            interaction: discord.Interaction,
            verified_role: Optional[discord.Role] = None,
            log_channel: Optional[discord.TextChannel] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            guild_id_str = str(interaction.guild.id)
            
            if not verified_role:
                verified_role = await BotManager.ensure_verified_role(bot_db_id, interaction.guild)
                if not verified_role:
                    await interaction.followup.send("❌ Failed to automatically create the Verified role. Please check bot role permissions.", ephemeral=True)
                    return

            async with async_session_factory() as db:
                stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == guild_id_str) & (GuildConfig.bot_id == bot_db_id)
                )
                res = await db.execute(stmt)
                cfg = res.scalars().first()
                if not cfg:
                    cfg = GuildConfig(
                        guild_id=guild_id_str,
                        bot_id=bot_db_id,
                        guild_name=interaction.guild.name,
                        guild_icon=str(interaction.guild.icon.url) if interaction.guild.icon else None,
                        firewall_enabled=True,
                        anti_vpn_enabled=True,
                    )
                    db.add(cfg)

                cfg.verified_role_id = str(verified_role.id)
                if log_channel:
                    cfg.log_channel_id = str(log_channel.id)
                await db.commit()

            net_info = detect_network_addresses()
            verify_url = f"{net_info['recommended_base_url']}/verify/{bot_db_id}/{guild_id_str}"

            embed = discord.Embed(
                title="✅ Verification Configured",
                description=f"Verification is active for **{interaction.guild.name}**!\n\n"
                            f"• **Verified Role**: {verified_role.mention}\n"
                            f"• **Log Channel**: {log_channel.mention if log_channel else 'None'}\n\n"
                            f"🔗 **Member Verification Link**:\n{verify_url}",
                color=0x57F287,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="verify_send", description="Send verification message with button to a channel.")
        @app_commands.describe(
            channel="Text channel to post the verify message in",
            title="Custom embed title (default: Server Verification)",
            description="Custom embed description (default: Click button below to verify)",
            button_label="Button text (default: Verify)",
        )
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def verify_send(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
            title: Optional[str] = "Server Verification",
            description: Optional[str] = "Click the button below to verify your account and gain access to the server.",
            button_label: Optional[str] = "Verify",
        ):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            target_channel = channel or interaction.channel
            if not isinstance(target_channel, discord.TextChannel):
                await interaction.followup.send("❌ Please select a text channel.", ephemeral=True)
                return

            guild_id_str = str(interaction.guild.id)
            net_info = detect_network_addresses()
            verify_url = f"{net_info['recommended_base_url']}/verify/{bot_db_id}/{guild_id_str}"

            await BotManager.ensure_verified_role(bot_db_id, interaction.guild)

            view = discord.ui.View(timeout=None)
            btn = discord.ui.Button(
                label=button_label or "Verify",
                url=verify_url,
                style=discord.ButtonStyle.link,
                emoji="🛡️",
            )
            view.add_item(btn)

            embed = discord.Embed(
                title=title or "Server Verification",
                description=description or "Click the button below to verify your account and gain access to the server.",
                color=0x5865F2,
            )
            if interaction.guild.icon:
                embed.set_footer(text=f"{interaction.guild.name} Verification", icon_url=str(interaction.guild.icon.url))
            else:
                embed.set_footer(text=f"{interaction.guild.name} Verification")

            try:
                await target_channel.send(embed=embed, view=view)
                await interaction.followup.send(f"✅ Verification message posted in {target_channel.mention}!", ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to post verify embed in slash command: {e}")
                await interaction.followup.send(f"❌ Failed to send verify message: {str(e)}", ephemeral=True)

        @self.tree.command(name="blacklist_add", description="Blacklist a Discord User ID from verification.")
        @app_commands.describe(user_id="Discord User ID", reason="Reason for blacklist")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def blacklist_add(interaction: discord.Interaction, user_id: str, reason: Optional[str] = "Manual Bot Blacklist"):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            async with async_session_factory() as db:
                bl = Blacklist(
                    type="USER_ID",
                    value=user_id,
                    reason=reason,
                    added_by=f"{interaction.user.name} ({interaction.user.id})",
                )
                db.add(bl)
                await db.execute(
                    update(MemberToken).where(MemberToken.user_id == user_id).values(is_blacklisted=True)
                )
                await db.commit()

            await interaction.followup.send(f"🛡️ User `<@{user_id}>` (`{user_id}`) added to global blacklist.", ephemeral=True)

        @self.tree.command(name="firewall_status", description="Display current security firewall rules for this server.")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def firewall_status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if not _is_guild_admin(interaction):
                await interaction.followup.send("❌ You must have Administrator permissions in this server to use this command.", ephemeral=True)
                return

            guild_id_str = str(interaction.guild.id)
            async with async_session_factory() as db:
                stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == guild_id_str) & (GuildConfig.bot_id == bot_db_id)
                )
                res = await db.execute(stmt)
                cfg = res.scalars().first()

            if not cfg:
                await interaction.followup.send("⚠️ No configuration found for this server.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"🛡️ Security Firewall Status: {interaction.guild.name}",
                color=0x5865F2,
            )
            embed.add_field(name="Firewall Enabled", value="✅ Yes" if cfg.firewall_enabled else "❌ No", inline=True)
            embed.add_field(name="Anti-VPN / Proxy", value="✅ Active" if cfg.anti_vpn_enabled else "❌ Disabled", inline=True)
            embed.add_field(name="Block Datacenter IPs", value="✅ Active" if cfg.block_datacenter else "❌ Disabled", inline=True)
            embed.add_field(name="Block Cellular / LTE", value="✅ Active" if cfg.block_cellular else "❌ Disabled", inline=True)
            embed.add_field(name="Min Account Age", value=f"`{cfg.min_account_age_days}` days", inline=True)
            embed.add_field(name="CAPTCHA Required", value=f"✅ {cfg.captcha_provider.capitalize()}" if cfg.captcha_enabled else "❌ None", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)


class BotManager:
    _instances: Dict[int, FreeCordBotClient] = {}
    _bot_tasks: Dict[int, asyncio.Task] = {}

    @classmethod
    def get_bot(cls, bot_db_id: int) -> Optional[FreeCordBotClient]:
        return cls._instances.get(bot_db_id)

    @classmethod
    def get_all_active_bots(cls) -> Dict[int, FreeCordBotClient]:
        return cls._instances

    @classmethod
    async def start_bot(cls, bot_db_id: int) -> bool:
        if bot_db_id in cls._instances and cls._instances[bot_db_id].is_ready():
            logger.info(f"Bot {bot_db_id} is already running.")
            return True

        async with async_session_factory() as db:
            stmt = select(Bot).where(Bot.id == bot_db_id)
            res = await db.execute(stmt)
            bot_record = res.scalar_one_or_none()

            if not bot_record:
                logger.error(f"Bot record {bot_db_id} does not exist.")
                return False

            raw_token = decrypt_secret(bot_record.token_encrypted)
            if not raw_token:
                logger.error(f"Bot token could not be decrypted for bot {bot_db_id}")
                return False

            client_id = bot_record.client_id
            custom_status = bot_record.custom_status or "FreeCord Security & Backup"

        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.guild_messages = True
        intents.message_content = True
        intents.emojis_and_stickers = True

        from core.proxy_manager import proxy_manager
        proxy_url = await proxy_manager.get_next_proxy() if (proxy_manager.custom_pool or proxy_manager.free_pool) else None

        bot_client = FreeCordBotClient(
            bot_db_id=bot_db_id,
            client_id=client_id,
            command_prefix="!fc ",
            intents=intents,
            help_command=None,
            proxy=proxy_url,
        )

        @bot_client.event
        async def on_ready():
            logger.info(f"Bot '{bot_client.user}' (DB ID: {bot_db_id}) is connected and ready.")
            try:
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=custom_status,
                )
                await bot_client.change_presence(status=discord.Status.online, activity=activity)
            except Exception as e:
                logger.warning(f"Error setting activity for bot {bot_db_id}: {e}")

            async with async_session_factory() as db:
                await db.execute(
                    update(Bot)
                    .where(Bot.id == bot_db_id)
                    .values(
                        status="ONLINE",
                        avatar_url=str(bot_client.user.display_avatar.url) if bot_client.user else None,
                        error_message=None,
                    )
                )
                await db.commit()

            for guild in bot_client.guilds:
                try:
                    await BotManager.ensure_verified_role(bot_db_id, guild)
                except Exception as err:
                    logger.warning(f"Could not auto-configure verified role in {guild.name}: {err}")

        @bot_client.event
        async def on_guild_join(guild: discord.Guild):
            logger.info(f"Bot {bot_db_id} entered server: {guild.name} ({guild.id})")
            try:
                role = await BotManager.ensure_verified_role(bot_db_id, guild)
                if role:
                    logger.info(f"Auto-configured @Verified role for server {guild.name} ({guild.id})")
            except Exception as e:
                logger.warning(f"Failed to auto-configure Verified role on guild join: {e}")

        @bot_client.event
        async def on_guild_remove(guild: discord.Guild):
            logger.warning(f"Bot {bot_db_id} removed from guild: {guild.name} ({guild.id})")
            async with async_session_factory() as db:
                stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == str(guild.id)) & (GuildConfig.bot_id == bot_db_id)
                )
                res = await db.execute(stmt)
                cfg = res.scalars().first()
                if cfg and cfg.auto_pull_enabled and cfg.auto_pull_backup_guild_id:
                    logger.info(f"Auto-pull triggered for backup server {cfg.auto_pull_backup_guild_id}")
                    import uuid
                    task_uuid = f"autopull_{uuid.uuid4().hex[:10]}"
                    pull_task = PullTask(
                        task_uuid=task_uuid,
                        bot_id=bot_db_id,
                        source_guild_id=str(guild.id),
                        target_guild_id=cfg.auto_pull_backup_guild_id,
                        status="PENDING",
                    )
                    db.add(pull_task)
                    await db.commit()
                    from services.migration_service import MigrationService
                    await MigrationService.start_pull_task(task_uuid)

        @bot_client.event
        async def on_member_ban(guild: discord.Guild, user: discord.User):
            async with async_session_factory() as db:
                stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == str(guild.id)) & (GuildConfig.bot_id == bot_db_id)
                )
                res = await db.execute(stmt)
                cfg = res.scalars().first()
                if cfg and getattr(cfg, "auto_blacklist_on_ban", True):
                    bl = Blacklist(
                        type="USER_ID",
                        value=str(user.id),
                        reason=f"Auto-blacklisted after ban from {guild.name}",
                        added_by="Bot Firewall",
                    )
                    db.add(bl)
                    await db.commit()
                    from services.security_service import SecurityService
                    await SecurityService.record_audit_log(
                        db=db,
                        event_type="AUTO_BLACKLIST_BAN",
                        guild_id=str(guild.id),
                        user_id=str(user.id),
                        description=f"Auto-blacklisted user {user.name} ({user.id}) after being banned from {guild.name}.",
                    )

        @bot_client.event
        async def on_member_remove(member: discord.Member):
            async with async_session_factory() as db:
                stmt = select(MemberToken).where(
                    (MemberToken.user_id == str(member.id)) & (MemberToken.bot_id == bot_db_id)
                )
                res = await db.execute(stmt)
                tokens = list(res.scalars().all())
                if tokens:
                    for tok in tokens:
                        tok.leave_count = (tok.leave_count or 0) + 1
                        tok.last_guild_left_at = datetime.utcnow()
                        db.add(tok)
                    await db.commit()

        cls._instances[bot_db_id] = bot_client

        async def _run_bot_task():
            try:
                await bot_client.start(raw_token)
            except Exception as e:
                logger.warning(f"Bot {bot_db_id} connection issue: {e}")
                async with async_session_factory() as db:
                    await db.execute(
                        update(Bot)
                        .where(Bot.id == bot_db_id)
                        .values(status="ERROR", error_message=str(e))
                    )
                    await db.commit()

        task = asyncio.create_task(_run_bot_task())
        cls._bot_tasks[bot_db_id] = task
        return True

    @classmethod
    async def stop_bot(cls, bot_db_id: int):
        if bot_db_id in cls._instances:
            client = cls._instances.pop(bot_db_id)
            try:
                await client.close()
            except Exception as e:
                logger.debug(f"Error closing bot client {bot_db_id}: {e}")

        if bot_db_id in cls._bot_tasks:
            task = cls._bot_tasks.pop(bot_db_id)
            task.cancel()

        async with async_session_factory() as db:
            await db.execute(
                update(Bot).where(Bot.id == bot_db_id).values(status="OFFLINE")
            )
            await db.commit()

    @classmethod
    async def restart_bot(cls, bot_db_id: int):
        await cls.stop_bot(bot_db_id)
        await asyncio.sleep(1)
        await cls.start_bot(bot_db_id)

    @classmethod
    async def start_all_active_bots(cls):
        logger.info("Booting registered active Discord bots...")
        async with async_session_factory() as db:
            stmt = select(Bot).where(Bot.is_active == True)
            res = await db.execute(stmt)
            bots = res.scalars().all()

        for b in bots:
            try:
                await cls.start_bot(b.id)
            except Exception as e:
                logger.error(f"Failed to start bot {b.name} (ID: {b.id}): {e}")

    @classmethod
    async def ensure_verified_role(cls, bot_db_id: int, guild: discord.Guild) -> Optional[discord.Role]:
        if not guild:
            return None

        target_role = None
        for r in guild.roles:
            if r.name.lower() == "verified":
                target_role = r
                break

        if not target_role:
            try:
                verified_color = discord.Color.from_rgb(88, 101, 242)
                target_role = await guild.create_role(
                    name="Verified",
                    color=verified_color,
                    reason="FreeCord Verification Role Setup",
                    mentionable=False,
                )
                logger.info(f"Created @Verified role (ID: {target_role.id}) in server '{guild.name}'")
            except Exception as e:
                logger.warning(f"Could not create role in server '{guild.name}': {e}")
                return None

        guild_id_str = str(guild.id)
        try:
            async with async_session_factory() as db:
                stmt = select(GuildConfig).where(
                    (GuildConfig.guild_id == guild_id_str) & (GuildConfig.bot_id == bot_db_id)
                )
                res = await db.execute(stmt)
                cfg = res.scalars().first()
                if not cfg:
                    cfg = GuildConfig(
                        bot_id=bot_db_id,
                        guild_id=guild_id_str,
                        guild_name=guild.name,
                        guild_icon=str(guild.icon.url) if guild.icon else None,
                        verified_role_id=str(target_role.id),
                        firewall_enabled=True,
                        anti_vpn_enabled=True,
                    )
                    db.add(cfg)
                else:
                    if not cfg.verified_role_id:
                        cfg.verified_role_id = str(target_role.id)
                    cfg.guild_name = guild.name
                    if guild.icon:
                        cfg.guild_icon = str(guild.icon.url)
                await db.commit()
        except Exception as err:
            logger.warning(f"Error saving GuildConfig for {guild_id_str}: {err}")

        return target_role

    ensure_green_verified_role = ensure_verified_role

    @classmethod
    async def assign_verified_role(
        cls,
        bot_db_id: int,
        guild_id: str,
        user_id: str,
        role_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Tuple[bool, str]:
        bot = cls.get_bot(bot_db_id)
        if not bot or not bot.is_ready():
            return False, "Bot is not currently online to assign roles."

        try:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                guild = await bot.fetch_guild(int(guild_id))
            if not guild:
                return False, f"Bot is not in target guild {guild_id}"

            role = None
            if role_id:
                try:
                    role = guild.get_role(int(role_id))
                except Exception:
                    role = None

            if not role:
                role = await cls.ensure_verified_role(bot_db_id, guild)

            if not role:
                return False, "Verified role not found and could not be created."

            member = guild.get_member(int(user_id))
            if not member:
                try:
                    member = await guild.fetch_member(int(user_id))
                except Exception:
                    member = None

            if not member and access_token:
                try:
                    bot_token = bot.http.token
                    headers = {
                        "Authorization": f"Bot {bot_token}",
                        "Content-Type": "application/json",
                    }
                    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
                    body = {
                        "access_token": access_token,
                        "roles": [str(role.id)],
                    }
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.put(url, headers=headers, json=body)
                        if resp.status_code in (201, 204):
                            return True, f"Joined server and assigned @{role.name} role."
                except Exception as join_err:
                    logger.warning(f"Could not join user to guild: {join_err}")

            if member:
                await member.add_roles(role, reason="FreeCord OAuth2 Verification Passed")
                return True, f"Role @{role.name} assigned successfully."
            else:
                return False, "User is not currently in the server."
        except discord.errors.Forbidden:
            return False, "Bot lacks permission to assign this role. Ensure the bot's role is positioned higher than the Verified role in Server Settings > Roles."
        except Exception as e:
            logger.error(f"Failed assigning role to {user_id} in {guild_id}: {e}")
            return False, str(e)

    @classmethod
    async def send_verify_embed_message(
        cls,
        bot_db_id: int,
        guild_id: str,
        channel_id: str,
        title: Optional[str] = "Server Verification",
        description: Optional[str] = "Click the button below to verify your account and gain access to the server.",
        button_label: Optional[str] = "Verify",
        color_hex: Optional[str] = "#5865F2",
    ) -> Tuple[bool, str]:
        bot = cls.get_bot(bot_db_id)
        if not bot or not bot.is_ready():
            return False, "Bot is not online right now"

        try:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                guild = await bot.fetch_guild(int(guild_id))
            if not guild:
                return False, "Bot is not in this server"

            channel = guild.get_channel(int(channel_id))
            if not channel:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except Exception:
                    channel = None

            if not channel or not isinstance(channel, discord.TextChannel):
                return False, "Channel not found or bot lacks permission"

            net_info = detect_network_addresses()
            verify_url = f"{net_info['recommended_base_url']}/verify/{bot_db_id}/{guild_id}"

            await cls.ensure_verified_role(bot_db_id, guild)

            view = discord.ui.View(timeout=None)
            btn = discord.ui.Button(
                label=button_label if button_label and button_label.strip() else "Verify",
                url=verify_url,
                style=discord.ButtonStyle.link,
                emoji="🛡️",
            )
            view.add_item(btn)

            color_int = 0x5865F2
            if color_hex:
                try:
                    clean_hex = color_hex.strip().lstrip("#")
                    color_int = int(clean_hex, 16)
                except Exception:
                    color_int = 0x5865F2

            embed = discord.Embed(
                title=title if title and title.strip() else "Server Verification",
                description=description if description and description.strip() else "Click the button below to verify your account and gain access to the server.",
                color=color_int,
            )
            if guild.icon:
                embed.set_footer(text=f"{guild.name} Verification", icon_url=str(guild.icon.url))
            else:
                embed.set_footer(text=f"{guild.name} Verification")

            await channel.send(embed=embed, view=view)
            return True, f"Verify message sent to #{channel.name}"
        except Exception as e:
            logger.error(f"Failed to send verify embed: {e}")
            return False, f"Could not send message: {str(e)}"

