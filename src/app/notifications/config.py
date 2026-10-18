from pydantic import Field

from app.schemas.pricing import FrozenSchema


class NotificationConfig(FrozenSchema):
    enabled: bool = False
    daily_limit: int = Field(default=50, ge=0, le=100000, strict=True)
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)
    backoff_seconds: float = Field(default=2, gt=0, le=60)
    max_backoff_seconds: float = Field(default=300, gt=0, le=86400)
    batch_size: int = Field(default=50, ge=1, le=1000, strict=True)
    sending_lease_seconds: int = Field(default=300, ge=120, le=86400, strict=True)
