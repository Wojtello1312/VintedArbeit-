"""Constant-time bearer authentication for all job information and controls."""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.scheduler.service import JobBusy, JobManager
from app.scheduler.store import CycleReport
from app.security import bearer_auth


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


def jobs_router(manager_provider: Callable[..., JobManager], token: SecretStr | None) -> APIRouter:
    # The provider is passed as a dependency so lifespan-owned state is resolved per request.
    router = APIRouter(prefix="/jobs", tags=["jobs"])
    authorize = bearer_auth(token)

    @router.get("/status", dependencies=[Depends(authorize)])
    async def status(
        manager: Annotated[JobManager, Depends(manager_provider)],
    ) -> dict[str, object]:
        return await manager.status()

    @router.post("/run", dependencies=[Depends(authorize)])
    async def run(
        manager: Annotated[JobManager, Depends(manager_provider)], request: RunRequest | None = None
    ) -> CycleReport:
        try:
            return await manager.run(None if request is None else request.run_id)
        except JobBusy as exc:
            raise HTTPException(409, "A cycle is already running") from exc

    @router.post("/pause", dependencies=[Depends(authorize)])
    async def pause(manager: Annotated[JobManager, Depends(manager_provider)]) -> dict[str, object]:
        await manager.pause()
        return await manager.status()

    @router.post("/resume", dependencies=[Depends(authorize)])
    async def resume(
        manager: Annotated[JobManager, Depends(manager_provider)],
    ) -> dict[str, object]:
        await manager.resume()
        return await manager.status()

    return router
