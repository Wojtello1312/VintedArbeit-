from datetime import UTC, datetime
from decimal import Decimal

from app.admin.queries import AdminQueries
from app.ai.budget import AIBudget
from app.ai.schemas import AIReview, StoredReview, TokenUsage


def test_ai_count_known_cost_and_unknown_reservations(settings):
    config = settings.ai.model_copy(
        update={
            "input_usd_per_million": Decimal("0.1"),
            "output_usd_per_million": Decimal("0.4"),
        }
    )
    budget = AIBudget(config)
    now = datetime.now(UTC)
    claim = budget.claim("test-known", "demo-test-model", "test-v1", now)
    usage = TokenUsage(input_tokens=1000, output_tokens=100, cached_input_tokens=0)
    cost = budget.settle(claim.attempt_id, usage)
    budget.finish(
        "test-known",
        StoredReview(
            input_hash="test-known",
            model="demo-test-model",
            prompt_version="test-v1",
            reviewed_at=now,
            result=AIReview(
                listing_consistency=80,
                risk_level="unknown",
                detected_issues=[],
                product_match_valid=True,
                manual_review_required=True,
                explanation="DEMO mocked result",
                confidence=0.8,
            ),
            usage=usage,
            cost_usd=cost,
            reserved_usd=budget.reservation_usd,
        ),
    )
    budget.claim("test-unknown", "demo-test-model", "test-v1", now)
    stats = AdminQueries.ai_totals(config.db_path)
    assert stats["ai_count"] == 1
    assert stats["ai_cost"] == Decimal("0.00014")
    assert stats["ai_unknown"] == 1
    assert stats["ai_reserved"] == budget.reservation_usd
    assert budget.claim("test-known", "demo-test-model", "test-v1", now).status == "cached"
    assert AdminQueries.ai_totals(config.db_path) == stats
