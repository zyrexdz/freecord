from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True)
    discord_id = Column(String(64), unique=True, nullable=True, index=True)
    avatar_url = Column(String(255), nullable=True)
    email = Column(String(128), nullable=True)
    role = Column(String(32), default="admin")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    client_id = Column(String(64), nullable=False, unique=True, index=True)
    client_secret_encrypted = Column(Text, nullable=False)
    token_encrypted = Column(Text, nullable=False)
    public_key = Column(String(128), nullable=True)
    status = Column(String(32), default="OFFLINE")
    custom_status = Column(String(128), default="FreeCord Security & Backup")
    status_activity_type = Column(String(32), default="WATCHING")
    avatar_url = Column(String(255), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_active = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    guild_configs = relationship("GuildConfig", back_populates="bot", cascade="all, delete-orphan")
    backups = relationship("Backup", back_populates="bot", cascade="all, delete-orphan")
    tokens = relationship("MemberToken", back_populates="bot", cascade="all, delete-orphan")
    pull_tasks = relationship("PullTask", back_populates="bot", cascade="all, delete-orphan")


class GuildConfig(Base):
    __tablename__ = "guild_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String(64), nullable=False, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    guild_name = Column(String(128), default="Discord Guild")
    guild_icon = Column(String(255), nullable=True)

    verified_role_id = Column(String(64), nullable=True)
    unverified_role_id = Column(String(64), nullable=True)
    log_channel_id = Column(String(64), nullable=True)
    webhook_url = Column(String(255), nullable=True)

    firewall_enabled = Column(Boolean, default=True)
    anti_vpn_enabled = Column(Boolean, default=True)
    block_cellular = Column(Boolean, default=False)
    block_datacenter = Column(Boolean, default=True)
    min_account_age_days = Column(Integer, default=7)
    captcha_enabled = Column(Boolean, default=False)
    captcha_provider = Column(String(32), default="turnstile")

    auto_pull_backup_guild_id = Column(String(64), nullable=True)
    auto_pull_enabled = Column(Boolean, default=False)
    auto_blacklist_on_ban = Column(Boolean, default=True)
    auto_kick_failed = Column(Boolean, default=False)
    backup_schedule = Column(String(32), default="OFF")
    max_backup_messages = Column(Integer, default=50)

    custom_branding_title = Column(String(128), default="Verification Portal")
    custom_branding_desc = Column(Text, default="Click below to securely verify your Discord account.")
    bg_image_url = Column(String(255), nullable=True)
    music_url = Column(String(255), nullable=True)
    theme_color = Column(String(32), default="#5865F2")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = relationship("Bot", back_populates="guild_configs")


class MemberToken(Base):
    __tablename__ = "member_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False, index=True)
    username = Column(String(128), nullable=False)
    discriminator = Column(String(32), default="0")
    email = Column(String(255), nullable=True)
    avatar = Column(String(255), nullable=True)

    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=True, default="")
    expires_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    scopes = Column(String(128), default="identify guilds.join email")

    ip_address = Column(String(64), nullable=True, index=True)
    country = Column(String(64), default="Unknown")
    country_code = Column(String(8), default="XX")
    city = Column(String(64), default="Unknown")
    isp = Column(String(128), default="Unknown")
    asn = Column(String(64), default="Unknown")
    user_agent = Column(Text, nullable=True)
    device_os = Column(String(64), default="Unknown")
    device_browser = Column(String(64), default="Unknown")
    device_type = Column(String(32), default="Desktop")
    extra_info_json = Column(Text, nullable=True)
    is_vpn = Column(Boolean, default=False)
    is_cellular = Column(Boolean, default=False)
    is_blacklisted = Column(Boolean, default=False)
    leave_count = Column(Integer, default=0)
    last_guild_left_at = Column(DateTime, nullable=True)

    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    source_guild_id = Column(String(64), nullable=True, index=True)

    verified_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = relationship("Bot", back_populates="tokens")

    __table_args__ = (
        Index("idx_user_bot", "user_id", "bot_id", unique=True),
    )


class Backup(Base):
    __tablename__ = "backups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backup_uuid = Column(String(64), unique=True, nullable=False, index=True)
    guild_id = Column(String(64), nullable=False, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    guild_name = Column(String(128), nullable=False)
    icon_url = Column(String(255), nullable=True)

    data_json = Column(Text, nullable=False)

    roles_count = Column(Integer, default=0)
    channels_count = Column(Integer, default=0)
    emojis_count = Column(Integer, default=0)
    stickers_count = Column(Integer, default=0)
    size_bytes = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    bot = relationship("Bot", back_populates="backups")


class PullTask(Base):
    __tablename__ = "pull_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(64), unique=True, nullable=False, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    source_guild_id = Column(String(64), nullable=True)
    target_guild_id = Column(String(64), nullable=False)

    status = Column(String(32), default="PENDING")
    total_members = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    already_in_guild_count = Column(Integer, default=0)
    rate_limited_count = Column(Integer, default=0)

    delay_ms = Column(Integer, default=1000)
    batch_size = Column(Integer, default=10)
    use_proxies = Column(Boolean, default=False)
    min_stay_days = Column(Integer, default=0)
    scheduled_for = Column(DateTime, nullable=True)

    logs = Column(Text, default="[]")
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = relationship("Bot", back_populates="pull_tasks")


class Blacklist(Base):
    __tablename__ = "blacklists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(32), default="USER_ID")
    value = Column(String(128), nullable=False, index=True)
    guild_id = Column(String(64), nullable=True, index=True)
    reason = Column(String(255), default="Manual Blacklist")
    added_by = Column(String(64), default="System")
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False)
    guild_id = Column(String(64), nullable=True)
    user_id = Column(String(64), nullable=True)
    description = Column(Text, nullable=False)
    metadata_json = Column(Text, default="{}")
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key = Column(String(64), primary_key=True)
    value_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BotCollaborator(Base):
    __tablename__ = "bot_collaborators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    user_id = Column(Integer, nullable=True)
    role_label = Column(String(32), default="Helper")

    can_manage_backups = Column(Boolean, default=True)
    can_start_migrations = Column(Boolean, default=True)
    can_view_member_details = Column(Boolean, default=True)
    can_manage_blacklist = Column(Boolean, default=True)
    can_manage_settings = Column(Boolean, default=False)
    can_export_tokens = Column(Boolean, default=False)

    allowed_guilds_json = Column(Text, default='["ALL"]')

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = relationship("Bot", backref="collaborators")

