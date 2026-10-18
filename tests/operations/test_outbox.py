import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.notifications.formatting import destination_key, make_alert
from app.notifications.outbox import AlertOutbox
from app.notifications.service import NotificationService
from app.notifications.telegram import DeliveryResult, TelegramNotifier
from app.schemas.deals import DealState


def test_once_across_analysis_versions_and_restarts(service, live_approved, notifier, moment):
    assert service.enqueue(live_approved) == "enqueued"
    assert service.enqueue(live_approved) == "duplicate"
    changed = live_approved.model_copy(update={"analysis_id": "another-analysis"})
    assert service.enqueue(changed) == "duplicate"
    assert asyncio.run(service.flush())["sent"] == 1
    assert service.pipeline.store.get(live_approved.analysis_id).state == DealState.NOTIFIED
    reopened = AlertOutbox(service.outbox.path, service.outbox.config)
    restarted = NotificationService(
        reopened, service.pipeline, notifier, service.destination, clock=lambda: moment[0]
    )
    assert restarted.enqueue(changed) == "duplicate"
    asyncio.run(restarted.flush())
    assert notifier.send.call_count == 1
    assert reopened.counts() == {"sent": 1}


def test_daily_limit_counts_sent_and_uncertain(outbox, live_approved, now):
    for index in range(3):
        message = make_alert(live_approved, destination_key(str(index)))
        outbox.enqueue(message, now)
        claim = outbox.claim(message.alert_key, now)
        if index < 2:
            assert claim is not None
            outbox.complete(claim, DeliveryResult(status="uncertain", reason="timeout"), now)
        else:
            assert claim is None  # Global limit, including across recipient changes.
    assert outbox.claim(message.alert_key, now + timedelta(days=1)) is not None


def test_parallel_claims_cannot_bypass_limit(outbox, live_approved, now):
    messages = [make_alert(live_approved, destination_key(str(i))) for i in range(8)]
    for message in messages:
        outbox.enqueue(message, now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda m: AlertOutbox(outbox.path, outbox.config).claim(m.alert_key, now), messages
            )
        )
    assert sum(item is not None for item in claims) == outbox.config.daily_limit


def test_same_alert_claimed_once(outbox, live_approved, now):
    message = make_alert(live_approved, destination_key("123"))
    outbox.enqueue(message, now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: outbox.claim(message.alert_key, now), range(12)))
    assert sum(item is not None for item in claims) == 1


def test_safe_retries_persist_backoff_and_release_slot(service, live_approved, notifier, moment):
    notifier.send.side_effect = [
        DeliveryResult(status="retry", reason="connect"),
        DeliveryResult(status="retry", reason="429", retry_after=10),
        DeliveryResult(status="sent", reason="ok", receipt_id="1"),
    ]
    service.enqueue(live_approved)
    asyncio.run(service.flush())
    assert service.outbox.daily_count(service.destination, moment[0]) == 0
    asyncio.run(service.flush())
    assert notifier.send.call_count == 1
    moment[0] += timedelta(seconds=2)
    asyncio.run(service.flush())
    assert notifier.send.call_count == 2
    moment[0] += timedelta(seconds=9)
    asyncio.run(service.flush())
    assert notifier.send.call_count == 2
    moment[0] += timedelta(seconds=1)
    asyncio.run(service.flush())
    assert notifier.send.call_count == 3
    assert service.outbox.daily_count(service.destination, moment[0]) == 1


def test_retry_limit_persists(service, live_approved, notifier, moment):
    notifier.send.return_value = DeliveryResult(status="retry", reason="connect")
    service.enqueue(live_approved)
    for _ in range(5):
        asyncio.run(service.flush())
        moment[0] += timedelta(seconds=100)
    assert notifier.send.call_count == 3
    assert service.outbox.counts() == {"failed": 1}


def test_unknown_delivery_never_retries(service, live_approved, notifier, moment):
    notifier.send.return_value = DeliveryResult(status="uncertain", reason="read_timeout")
    service.enqueue(live_approved)
    asyncio.run(service.flush())
    moment[0] += timedelta(days=2)
    asyncio.run(service.flush())
    assert notifier.send.call_count == 1
    assert service.outbox.counts() == {"uncertain": 1}
    assert service.pipeline.store.get(live_approved.analysis_id).state == DealState.APPROVED


def test_crashed_sending_recovered_as_uncertain(outbox, live_approved, now):
    message = make_alert(live_approved, destination_key("123"))
    outbox.enqueue(message, now)
    assert outbox.claim(message.alert_key, now) is not None
    restarted = AlertOutbox(outbox.path, outbox.config)
    restarted.recover(now + timedelta(hours=1))
    assert restarted.counts() == {"uncertain": 1}
    assert restarted.claim(message.alert_key, now + timedelta(hours=1)) is None
    assert restarted.daily_count(message.destination, now) == 1


def test_receipt_checkpoint_failure_does_not_resend(service, live_approved, notifier, moment):
    service.enqueue(live_approved)
    original = service.pipeline.mark_notified
    service.pipeline.mark_notified = Mock(side_effect=RuntimeError("database unavailable"))
    assert asyncio.run(service.flush())["checkpoint_pending"] == 1
    service.pipeline.mark_notified = original
    asyncio.run(service.flush())
    assert notifier.send.call_count == 1
    assert service.pipeline.store.get(live_approved.analysis_id).state == DealState.NOTIFIED


def test_demo_local_sink_ignores_live_credentials(service, approved, notifier):
    service.enqueue(approved)
    assert asyncio.run(service.flush())["sent"] == 1
    notifier.send.assert_not_called()
    record = service.pipeline.store.get(approved.analysis_id)
    assert record.state == DealState.NOTIFIED and record.notification_receipt.channel == "demo"


def test_no_token_keeps_pending_and_reserves_nothing(service, live_approved, moment):
    service.telegram = TelegramNotifier(service.outbox.config, None, None, client=AsyncMock())
    service.enqueue(live_approved)
    assert asyncio.run(service.flush()) == {"disabled": 1}
    assert service.outbox.counts() == {"pending": 1}
    assert service.outbox.daily_count(service.destination, moment[0]) == 0


def test_mock_cycle_cannot_flush_existing_production_queue(service, live_approved, notifier):
    service.enqueue(live_approved)
    assert asyncio.run(service.flush(allow_production=False)) == {}
    notifier.send.assert_not_called()


def test_cancellation_keeps_reservation(service, live_approved, notifier, moment):
    notifier.send.side_effect = asyncio.CancelledError()
    service.enqueue(live_approved)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.flush())
    assert service.outbox.counts() == {"sending": 1}
    moment[0] += timedelta(hours=1)
    asyncio.run(service.flush())
    assert service.outbox.counts() == {"uncertain": 1}
    assert notifier.send.call_count == 1


def test_old_recipient_queue_does_not_starve_current_recipient(service, live_approved, moment):
    for index in range(service.outbox.config.batch_size + 1):
        service.outbox.enqueue(
            make_alert(live_approved, destination_key(f"old-{index}")), moment[0]
        )
    service.enqueue(live_approved)
    assert asyncio.run(service.flush())["sent"] == 1
    assert service.telegram.send.call_count == 1


def test_zero_limit_and_oversized_retry_after(service, live_approved, notifier, moment):
    service.outbox.config = service.outbox.config.model_copy(update={"daily_limit": 0})
    service.enqueue(live_approved)
    asyncio.run(service.flush())
    notifier.send.assert_not_called()
    service.outbox.config = service.outbox.config.model_copy(update={"daily_limit": 2})
    notifier.send.return_value = DeliveryResult(status="retry", reason="429", retry_after=86400)
    assert asyncio.run(service.flush())["failed"] == 1
    assert service.outbox.daily_count(None, moment[0]) == 0
