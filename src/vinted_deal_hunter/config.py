import os
import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.admin.config import AdminConfig
from app.admin.password import valid_hash
from app.ai.config import AIConfig
from app.config import PricingConfig
from app.deal_config import DealConfig
from app.notifications.config import NotificationConfig
from app.scheduler.config import SchedulerConfig
from vinted_deal_hunter.vinted_playwright import VintedAutomationConfig


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    db_path: Path = Path(".data/offers.sqlite3")
    max_import_bytes: int = Field(default=2_000_000, ge=1, le=100_000_000)
    max_import_records: int = Field(default=10_000, ge=1, le=1_000_000)
    max_report_errors: int = Field(default=100, ge=1, le=10_000)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    deal: DealConfig = Field(default_factory=DealConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    vinted: VintedAutomationConfig = Field(default_factory=VintedAutomationConfig)
    telegram_bot_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    chat_id: SecretStr | None = Field(default=None, exclude=True, repr=False)
    jobs_api_token: SecretStr | None = Field(default=None, exclude=True, repr=False, min_length=32)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    admin_password_hash: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def database_locations(self) -> Self:
        if "db_path" not in self.ai.model_fields_set:
            object.__setattr__(
                self,
                "ai",
                self.ai.model_copy(update={"db_path": self.db_path.with_name("ai.sqlite3")}),
            )
        paths = [
            self.db_path,
            self.db_path.with_name("deals.sqlite3"),
            self.jobs_db_path,
            self.ai.db_path,
        ]
        if len({path.resolve() for path in paths}) != len(paths):
            raise ValueError("Database roles must have distinct paths")
        return self

    @field_validator("admin_password_hash")
    @classmethod
    def admin_hash(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not valid_hash(value.get_secret_value()):
            raise ValueError("invalid administrator password hash")
        return value

    @field_validator("admin", mode="before")
    @classmethod
    def parse_admin(cls, value: object) -> object:
        return AdminConfig.model_validate_json(value) if isinstance(value, str) else value

    @field_validator("telegram_bot_token")
    @classmethod
    def valid_bot_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[0-9]+:[A-Za-z0-9_-]+", value.get_secret_value()
        ):
            raise ValueError("invalid Telegram token format")
        return value

    @field_validator("chat_id")
    @classmethod
    def valid_chat(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,}", value.get_secret_value()
        ):
            raise ValueError("invalid Telegram chat identifier")
        return value

    @field_validator("notifications", mode="before")
    @classmethod
    def parse_notifications(cls, value: object) -> object:
        return NotificationConfig.model_validate_json(value) if isinstance(value, str) else value

    @field_validator("scheduler", mode="before")
    @classmethod
    def parse_scheduler(cls, value: object) -> object:
        return SchedulerConfig.model_validate_json(value) if isinstance(value, str) else value

    @field_validator("vinted", mode="before")
    @classmethod
    def parse_vinted(cls, value: object) -> object:
        return (
            VintedAutomationConfig.model_validate_json(value) if isinstance(value, str) else value
        )

    @property
    def jobs_db_path(self) -> Path:
        return self.scheduler.db_path or self.db_path.with_name("jobs.sqlite3")

    @field_validator("ai", mode="before")
    @classmethod
    def parse_ai_json(cls, value: object) -> object:
        return AIConfig.model_validate_json(value) if isinstance(value, str) else value

    @field_validator("deal", mode="before")
    @classmethod
    def parse_deal_json(cls, value: object) -> object:
        return DealConfig.model_validate_json(value) if isinstance(value, str) else value

    @field_validator("pricing", mode="before")
    @classmethod
    def parse_pricing_json(cls, value: object) -> object:
        return PricingConfig.model_validate_json(value) if isinstance(value, str) else value

    @classmethod
    def from_env(cls) -> "Settings":
        values: dict[str, object] = {
            key: os.environ[f"VDH_{key.upper()}"]
            for key in cls.model_fields
            if f"VDH_{key.upper()}" in os.environ
        }
        values["ai"] = AIConfig.from_env()
        for field, variable in (
            ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
            ("chat_id", "CHAT_ID"),
            ("jobs_api_token", "JOBS_API_TOKEN"),
            ("admin_password_hash", "ADMIN_PASSWORD_HASH"),
        ):
            if os.environ.get(variable):
                values[field] = os.environ[variable]
        configured_ai = values["ai"]
        assert isinstance(configured_ai, AIConfig)
        legacy_ledger = Path(".data/ai.sqlite3")
        offer_path = Path(str(values.get("db_path", ".data/offers.sqlite3")))
        if (
            "db_path" not in configured_ai.model_fields_set
            and legacy_ledger.exists()
            and legacy_ledger.resolve() != offer_path.with_name("ai.sqlite3").resolve()
        ):
            raise ValueError(
                "Existing legacy AI ledger detected. Set VDH_AI.db_path explicitly "
                "to retain its budgets before changing the data directory."
            )
        return cls.model_validate(values)
