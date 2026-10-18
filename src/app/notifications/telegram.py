"""Async Telegram transport. Ambiguous deliveries are never automatically retried."""

import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.notifications.config import NotificationConfig
from app.notifications.formatting import AlertMessage, destination_key
from app.schemas.pricing import FrozenSchema


class DeliveryResult(FrozenSchema):
    status: Literal["sent", "retry", "failed", "uncertain"]
    reason: str
    receipt_id: str | None = None
    retry_after: float = Field(default=0, ge=0)


class Notifier(Protocol):
    @property
    def ready(self) -> bool: ...

    async def send(self, message: AlertMessage) -> DeliveryResult: ...


class TelegramReply(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    ok: bool = Field(strict=True)
    result: dict[str, object] | None = None
    error_code: int | None = Field(default=None, strict=True)
    parameters: dict[str, object] | None = None


class TokenRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs request URLs at INFO; Telegram embeds its credential in the path.
        import re

        record.msg = re.sub(
            r"/bot[0-9]+(?::|%3[Aa])[^/\s]+/", "/bot[REDACTED]/", record.getMessage()
        )
        record.args = ()
        return True


_REDACTOR = TokenRedactor()


def retry_after_header(value: str | None) -> float:
    if value is None:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if seconds < float("inf") else float("inf")
    except ValueError:
        try:
            return max(
                0,
                (parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return 0


class TelegramNotifier:
    def __init__(
        self,
        config: NotificationConfig,
        token: SecretStr | None,
        chat_id: SecretStr | None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._owns_client = client is None
        for name in (
            "httpx",
            "httpcore",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.connection",
            "httpcore.proxy",
        ):
            logging.getLogger(name).addFilter(_REDACTOR)

    @property
    def ready(self) -> bool:
        return self.config.enabled and self._token is not None and self._chat_id is not None

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def send(self, message: AlertMessage) -> DeliveryResult:
        if message.is_demo:
            return DeliveryResult(status="failed", reason="demo_must_not_reach_telegram")
        if not self.ready:
            return DeliveryResult(status="failed", reason="telegram_not_configured")
        assert self._token is not None and self._chat_id is not None
        if message.destination != destination_key(self._chat_id.get_secret_value()):
            return DeliveryResult(status="failed", reason="recipient_mismatch")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.timeout_seconds, follow_redirects=False
            )
        try:
            response = await self._client.post(
                f"https://api.telegram.org/bot{self._token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": self._chat_id.get_secret_value(),
                    "text": message.html,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=self.config.timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return DeliveryResult(status="retry", reason="connection_not_established")
        except httpx.TransportError:
            return DeliveryResult(status="uncertain", reason="delivery_unknown")
        try:
            reply = TelegramReply.model_validate(response.json())
        except (ValidationError, ValueError):
            return DeliveryResult(status="uncertain", reason="invalid_telegram_response")
        if reply.ok and response.is_success and reply.result is not None:
            message_id = reply.result.get("message_id")
            if type(message_id) is int and message_id > 0:
                return DeliveryResult(
                    status="sent", reason="telegram_confirmed", receipt_id=str(message_id)
                )
        if not reply.ok and reply.error_code is not None:
            if reply.error_code == 429 or reply.error_code >= 500:
                delay = (reply.parameters or {}).get("retry_after", 0)
                if not isinstance(delay, (int, float)) or isinstance(delay, bool):
                    return DeliveryResult(status="failed", reason="invalid_retry_after")
                try:
                    return DeliveryResult(
                        status="retry",
                        reason=f"telegram_{reply.error_code}",
                        retry_after=max(
                            delay, retry_after_header(response.headers.get("retry-after"))
                        ),
                    )
                except ValidationError:
                    return DeliveryResult(status="failed", reason="invalid_retry_after")
            return DeliveryResult(status="failed", reason=f"telegram_{reply.error_code}")
        return DeliveryResult(status="uncertain", reason="unconfirmed_delivery")


class DemoNotifier:
    @property
    def ready(self) -> bool:
        return True

    async def send(self, message: AlertMessage) -> DeliveryResult:
        if not message.is_demo:
            return DeliveryResult(status="failed", reason="production_must_not_use_demo_sink")
        return DeliveryResult(
            status="sent", reason="demo_local_only", receipt_id=f"demo:{message.alert_key}"
        )
