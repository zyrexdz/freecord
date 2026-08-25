import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select

from core.config import get_settings
from database.models import Base, User
from core.security import get_password_hash

logger = logging.getLogger("freecord.database")

settings = get_settings()

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        columns_to_ensure = [
            ("guild_configs", "auto_blacklist_on_ban", "BOOLEAN DEFAULT 1"),
            ("guild_configs", "auto_kick_failed", "BOOLEAN DEFAULT 0"),
            ("guild_configs", "backup_schedule", "VARCHAR(32) DEFAULT 'OFF'"),
            ("guild_configs", "max_backup_messages", "INTEGER DEFAULT 50"),
            ("member_tokens", "leave_count", "INTEGER DEFAULT 0"),
            ("member_tokens", "last_guild_left_at", "DATETIME"),
            ("member_tokens", "user_agent", "TEXT"),
            ("member_tokens", "device_os", "VARCHAR(64) DEFAULT 'Unknown'"),
            ("member_tokens", "device_browser", "VARCHAR(64) DEFAULT 'Unknown'"),
            ("member_tokens", "device_type", "VARCHAR(32) DEFAULT 'Desktop'"),
            ("member_tokens", "extra_info_json", "TEXT"),
            ("pull_tasks", "min_stay_days", "INTEGER DEFAULT 0"),
            ("pull_tasks", "scheduled_for", "DATETIME"),
            ("blacklists", "guild_id", "VARCHAR(64)"),
            ("bots", "owner_id", "INTEGER"),
            ("users", "discord_id", "VARCHAR(64)"),
            ("users", "avatar_url", "VARCHAR(255)"),
            ("users", "email", "VARCHAR(128)"),
        ]
        for table, col, col_type in columns_to_ensure:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                pass

        try:
            await conn.execute(text("UPDATE bots SET owner_id = 1 WHERE owner_id IS NULL"))
        except Exception:
            pass

    async with async_session_factory() as session:
        stmt = select(User).where(User.username == settings.ADMIN_USERNAME)
        result = await session.execute(stmt)
        admin = result.scalar_one_or_none()
        if not admin:
            new_admin = User(
                username=settings.ADMIN_USERNAME,
                password_hash=get_password_hash(settings.ADMIN_PASSWORD),
                role="admin",
                is_active=True,
            )
            session.add(new_admin)
            await session.commit()
        else:
            admin.password_hash = get_password_hash(settings.ADMIN_PASSWORD)
            await session.commit()
