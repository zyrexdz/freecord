import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from core.config import get_settings, detect_network_addresses
from database.session import init_db
from services.bot_manager import BotManager

from web.routes.auth import router as auth_router
from web.routes.dashboard import router as dashboard_router
from web.routes.bots import router as bots_router
from web.routes.servers import router as servers_router
from web.routes.oauth import router as oauth_router
from web.routes.backups import router as backups_router
from web.routes.migrations import router as migrations_router
from web.routes.analytics import router as analytics_router
from web.routes.setup_guide import router as setup_guide_router
from web.routes.settings import router as settings_router

logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.ERROR)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)

logger = logging.getLogger("freecord.web")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "css").mkdir(exist_ok=True)
(STATIC_DIR / "js").mkdir(exist_ok=True)


from services.migration_service import MigrationService


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await BotManager.start_all_active_bots()
    MigrationService.start_schedule_worker()
    yield
    active_bots = list(BotManager.get_all_active_bots().keys())
    for bot_id in active_bots:
        await BotManager.stop_bot(bot_id)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="FreeCord",
        description="Self-Hosted Multi-Bot Discord Backup, Migration & Security Platform",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(bots_router)
    app.include_router(servers_router)
    app.include_router(oauth_router)
    app.include_router(backups_router)
    app.include_router(migrations_router)
    app.include_router(analytics_router)
    app.include_router(setup_guide_router)
    app.include_router(settings_router)

    @app.get("/api/health")
    async def health_check():
        return {"status": "ok", "app": "FreeCord"}

    return app



app = create_app()
