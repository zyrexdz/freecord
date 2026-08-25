import logging
from typing import Dict, Any, Optional
from services.migration_engine import MigrationEngine

logger = logging.getLogger("freecord.migration_service")


class MigrationService:

    @staticmethod
    def register_ws(task_uuid: str, ws: Any):
        MigrationEngine.register_ws(task_uuid, ws)

    @staticmethod
    def unregister_ws(task_uuid: str, ws: Any):
        MigrationEngine.unregister_ws(task_uuid, ws)

    @staticmethod
    def get_task_state(task_uuid: str) -> Optional[Dict[str, Any]]:
        return MigrationEngine.get_state(task_uuid)

    @staticmethod
    async def broadcast_task_update(task_uuid: str, payload: Dict[str, Any]):
        await MigrationEngine.broadcast(task_uuid, payload)

    @classmethod
    async def start_pull_task(cls, task_uuid: str, limit_count: Optional[int] = None, min_stay_days: int = 0) -> None:
        await MigrationEngine.start(task_uuid, limit_count=limit_count, min_stay_days=min_stay_days)

    @classmethod
    def pause_task(cls, task_uuid: str):
        MigrationEngine.pause(task_uuid)

    @classmethod
    def resume_task(cls, task_uuid: str):
        MigrationEngine.resume(task_uuid)

    @classmethod
    def stop_task(cls, task_uuid: str):
        MigrationEngine.stop(task_uuid)

    @classmethod
    def start_schedule_worker(cls):
        import asyncio
        return asyncio.create_task(MigrationEngine.schedule_worker())
