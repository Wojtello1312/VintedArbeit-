from pathlib import Path

from pydantic import Field

from app.schemas.pricing import FrozenSchema


class SchedulerConfig(FrozenSchema):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=1, le=86400, strict=True)
    db_path: Path | None = None
