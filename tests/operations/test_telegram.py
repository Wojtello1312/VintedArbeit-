import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.notifications.config import NotificationConfig
from app.notifications.formatting import destination_key, make_alert
from app.notifications.telegram import TelegramNotifier
from app.schemas.deals import DealSnapshot
from vinted_deal_hunter.config import Settings

TOKEN = SecretStr("12345:NOT_A_REAL_TOKEN")
CHAT = SecretStr("123")


def test_html_contains_facts_and_escapes_listing(live_approved):
    snapshot = live_approved.request.snapshot.model_copy(
        update={
            "offer": live_approved.request.snapshot.offer.model_copy(
                update={"product_name": '<script>evil & "title"</script>'}
            )
        }
    )
    record = live_approved.model_copy(
        update={"request": live_approved.request.model_copy(update={"snapshot": snapshot})}
    )
    message = make_alert(record, destination_key("123"))
    assert "<script>" not in message.html
    assert "&lt;script&gt;" in message.html and "&amp;" in message.html
    assert 'href="https://example.com/DEMO/item?x=1&amp;y=2"' in message.html
    for value in (
        "Zakup:",
        "Cena referencyjna:",
        "Szacunkowy zysk:",
        "Scoring:",
        "Confidence wyceny:",
        "Risk level:",
        "Kwalifikacja:",
        "sold",
        "próba:",
    ):
        assert value in message.html
    assert str(record.profit.estimated_profit) in message.html


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "https://example.com/ bad",
        "https://example.com:bad/a",
        "//example.com/a",
    ],
)
def test_invalid_links_rejected(url, request_data):
    with pytest.raises(ValueError):
        DealSnapshot.model_validate({**request_data.snapshot.model_dump(), "listing_url": url})


def test_missing_link_not_fabricated(approved):
    record = approved.model_copy(
        update={
            "request": approved.request.model_copy(
                update={
                    "snapshot": approved.request.snapshot.model_copy(update={"listing_url": None})
                }
            )
        }
    )
    with pytest.raises(ValueError, match="URL is required"):
        make_alert(record, "demo")


def test_missing_token_no_network(notification_config, live_approved):
    client = AsyncMock()
    notifier = TelegramNotifier(notification_config, None, CHAT, client=client)
    assert not notifier.ready
    result = asyncio.run(notifier.send(make_alert(live_approved, destination_key("123"))))
    assert result.reason == "telegram_not_configured"
    client.post.assert_not_called()


def test_success_html_timeout_and_secret_redaction(notification_config, live_approved, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        data = json.loads(request.content)
        assert request.url.host == "api.telegram.org"
        assert data["parse_mode"] == "HTML"
        assert data["chat_id"] == "123"
        assert data["link_preview_options"]["is_disabled"] is True
        assert request.extensions["timeout"]["read"] == notification_config.timeout_seconds
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            notifier = TelegramNotifier(notification_config, TOKEN, CHAT, client=client)
            return await notifier.send(make_alert(live_approved, destination_key("123")))

    caplog.set_level(logging.INFO)
    result = asyncio.run(run())
    assert result.status == "sent" and result.receipt_id == "7"
    assert len(calls) == 1
    assert TOKEN.get_secret_value() not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.ConnectError, "retry"),
        (httpx.ConnectTimeout, "retry"),
        (httpx.PoolTimeout, "retry"),
        (httpx.ReadTimeout, "uncertain"),
        (httpx.WriteTimeout, "uncertain"),
        (httpx.ReadError, "uncertain"),
    ],
)
def test_network_failure_classification(error, status, notification_config, live_approved):
    def handler(request):
        raise error("private text including credentials", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TelegramNotifier(notification_config, TOKEN, CHAT, client=client).send(
                make_alert(live_approved, destination_key("123"))
            )

    assert asyncio.run(run()).status == status


@pytest.mark.parametrize(
    "http_status,body,status",
    [
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}, "retry"),
        (500, {"ok": False, "error_code": 500}, "retry"),
        (403, {"ok": False, "error_code": 403}, "failed"),
        (400, {"ok": False, "error_code": 400}, "failed"),
        (200, {"ok": True, "result": {}}, "uncertain"),
        (200, {"ok": True, "result": {"message_id": True}}, "uncertain"),
        (500, {"unknown": "maybe sent"}, "uncertain"),
        (200, {"ok": "true", "result": {"message_id": 3}}, "uncertain"),
    ],
)
def test_api_failures(http_status, body, status, notification_config, live_approved):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(http_status, json=body))
        ) as client:
            return await TelegramNotifier(notification_config, TOKEN, CHAT, client=client).send(
                make_alert(live_approved, destination_key("123"))
            )

    result = asyncio.run(run())
    assert result.status == status
    if http_status == 429:
        assert result.retry_after == 5


def test_demo_blocked_even_with_credentials(notification_config, approved):
    client = AsyncMock()
    notifier = TelegramNotifier(notification_config, TOKEN, CHAT, client=client)
    result = asyncio.run(notifier.send(make_alert(approved, "demo")))
    assert result.reason == "demo_must_not_reach_telegram"
    client.post.assert_not_called()


def test_credentials_are_environment_only_and_redacted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN.get_secret_value())
    monkeypatch.setenv("CHAT_ID", "-100123")
    monkeypatch.setenv("JOBS_API_TOKEN", "test-" + "a" * 32)
    monkeypatch.setenv("VDH_NOTIFICATIONS", '{"enabled":true,"daily_limit":3}')
    settings = Settings.from_env()
    assert settings.telegram_bot_token == TOKEN
    assert settings.chat_id.get_secret_value() == "-100123"
    assert settings.notifications.daily_limit == 3
    assert TOKEN.get_secret_value() not in settings.model_dump_json()
    assert TOKEN.get_secret_value() not in repr(settings)
    with pytest.raises(ValidationError) as error:
        Settings(telegram_bot_token="TOP_SECRET_INVALID_TOKEN")
    assert "TOP_SECRET_INVALID_TOKEN" not in str(error.value)
    with pytest.raises(ValidationError):
        Settings(jobs_api_token="short")
    assert not NotificationConfig().enabled


def test_retry_after_header_honored(notification_config, live_approved):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429, json={"ok": False, "error_code": 429}, headers={"Retry-After": "12"}
                )
            )
        ) as client:
            return await TelegramNotifier(notification_config, TOKEN, CHAT, client=client).send(
                make_alert(live_approved, destination_key("123"))
            )

    assert asyncio.run(run()).retry_after == 12
