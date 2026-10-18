from pydantic import Field

from app.schemas.pricing import FrozenSchema


class AdminConfig(FrozenSchema):
    username: str = Field(default="admin", min_length=1, max_length=100)
    secure_cookies: bool = False
    session_seconds: int = Field(default=28800, ge=300, le=86400)
    login_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=30, le=3600)
