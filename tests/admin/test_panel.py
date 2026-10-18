from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.admin.queries import AdminQueries, OfferFilters, offer_key
from app.core.deal_pipeline import DealPipeline
from tests.admin.helpers import csrf, login
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.db import SQLiteOfferRepository


@pytest.mark.parametrize("path", ["", "/offers", "/deals", "/research", "/settings", "/system"])
def test_empty_pages_require_login_and_render(signed_in, settings, path):
    response = signed_in.get("/admin" + path)
    assert response.status_code == 200
    assert '<html lang="pl">' in response.text
    for secret in (
        settings.telegram_bot_token,
        settings.jobs_api_token,
        settings.chat_id,
        settings.admin_password_hash,
    ):
        assert secret.get_secret_value() not in response.text
    assert "pbkdf2_sha256" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_dashboard_and_details_use_saved_analysis(signed_in, seeded, settings):
    queries = AdminQueries(settings)
    stats = queries.dashboard()
    assert stats["total"] == stats["deals"] == stats["demo_count"] == 1
    assert stats["new"] == stats["rejected"] == stats["sent"] == stats["ai_count"] == 0
    assert stats["avg_score"] == seeded.score.total
    offer = seeded.request.snapshot.offer
    key = offer_key(offer.source_name, offer.external_id)
    detail = signed_in.get(f"/admin/offers/{key}")
    assert detail.status_code == 200
    assert "DEMO — dane fikcyjne" in detail.text
    assert seeded.scoring_version in detail.text
    assert str(seeded.profit.estimated_profit) in detail.text
    assert "Znormalizowana" in detail.text
    research = signed_in.get("/admin/research", params={"key": key})
    assert research.status_code == 200
    assert str(seeded.estimate.sample_size) in research.text
    assert "SOLD — potwierdzona" in research.text
    assert seeded.estimate.estimation_method in research.text
    assert "W próbie wyceny" in research.text


def test_latest_analysis_counts_once_and_history_keeps_both(signed_in, seeded, settings):
    request = seeded.request.model_copy(update={"as_of": seeded.request.as_of + timedelta(days=1)})
    DealPipeline(signed_in.app.state.runtime.analysis_store).process(request)
    queries = AdminQueries(settings)
    assert queries.dashboard()["total"] == queries.dashboard()["deals"] == 1
    records, total = queries.history(request.snapshot.offer)
    assert total == 2 and records[0].request.as_of == request.as_of


def test_filter_sort_pagination_decimal_and_currency(signed_in, seeded, settings):
    template = seeded.request.snapshot.offer
    offers = [
        template.model_copy(
            update={
                "external_id": f"DEMO-filter-{index:02}",
                "product_name": f"Test DEMO {index:02}",
                "asking_price": Decimal("100.01") + index,
            }
        )
        for index in range(30)
    ]
    offers.append(
        template.model_copy(
            update={
                "external_id": "DEMO-eur",
                "currency": "EUR",
                "product_name": "Only EUR DEMO",
            }
        )
    )
    repo = SQLiteOfferRepository(settings.db_path)
    repo.add_many(offers)
    repo.close()
    queries = AdminQueries(settings)
    rows, total = queries.offers(OfferFilters(q="test demo", sort="price_asc"))
    assert total == 30 and len(rows) == 25
    assert rows[0].offer.asking_price == Decimal("100.01")
    rows, total = queries.offers(OfferFilters(q="test demo", page=2, sort="price_asc"))
    assert len(rows) == 5 and rows[0].offer.asking_price == Decimal("125.01")
    assert queries.offers(OfferFilters(q="test demo", min_price=Decimal("100.02")))[1] == 29
    assert queries.offers(OfferFilters(q="test demo", max_price=Decimal("100.01")))[1] == 1
    assert queries.offers(OfferFilters(currency="EUR"))[1] == 1
    assert queries.offers(OfferFilters(brand=template.brand))[1] == 31
    assert queries.offers(OfferFilters(brand="missing"))[1] == 0
    assert queries.offers(OfferFilters(risk="unknown"))[1] == 30
    assert queries.offers(OfferFilters(min_score=seeded.score.total))[1] == 1
    assert (
        queries.offers(OfferFilters(min_profit=seeded.profit.estimated_profit), deals=True)[1] == 1
    )
    assert (
        queries.offers(OfferFilters(min_profit=seeded.profit.estimated_profit + 1), deals=True)[1]
        == 0
    )
    assert queries.offers(OfferFilters(mode="production"))[1] == 0
    assert queries.offers(OfferFilters(q="' OR 1=1--"))[1] == 0
    partial = signed_in.get(
        "/admin/offers?q=test&min_price=&max_price=&min_score=&min_profit=",
        headers={"HX-Request": "true"},
    )
    assert partial.status_code == 200
    assert "<html" not in partial.text and 'id="results"' in partial.text
    assert signed_in.get("/admin/offers?sort=DROP%20TABLE").status_code == 422
    assert signed_in.get("/admin/research?q=only&currency=EUR").status_code == 200


@pytest.mark.parametrize("label", ["good_deal", "bad_deal", "purchased", "ignored"])
def test_feedback_persists_without_mutating_analysis(signed_in, seeded, settings, label):
    offer = seeded.request.snapshot.offer
    key = offer_key(offer.source_name, offer.external_id)
    token = csrf(signed_in.get(f"/admin/offers/{key}"))
    response = signed_in.post(f"/admin/offers/{key}/feedback", data={"csrf": token, "label": label})
    assert response.status_code == 200
    view = AdminQueries(settings).offer(key)
    assert view.label == label and view.analysis == seeded
    assert AdminQueries(settings).offers(OfferFilters(label=label), deals=True)[1] == 1
    with TestClient(create_app(settings)) as restarted:
        login(restarted)
        assert "Historia ocen ręcznych" in restarted.get(f"/admin/offers/{key}").text
        assert AdminQueries(settings).offer(key).label == label


def test_listing_html_is_escaped(signed_in, seeded, settings):
    snapshot = seeded.request.snapshot
    attack = '<script>alert("xss")</script>'
    offer = snapshot.offer.model_copy(update={"external_id": "DEMO-xss", "product_name": attack})
    repo = SQLiteOfferRepository(settings.db_path)
    repo.add_many([offer])
    repo.close()
    request = seeded.request.model_copy(
        update={
            "snapshot": snapshot.model_copy(
                update={
                    "offer": offer,
                    "description": attack,
                }
            )
        }
    )
    DealPipeline(signed_in.app.state.runtime.analysis_store).process(request)
    response = signed_in.get(f"/admin/offers/{offer_key(offer.source_name, offer.external_id)}")
    assert response.status_code == 200
    assert attack not in response.text and "&lt;script&gt;" in response.text


def test_local_assets_and_missing_offer(signed_in):
    for asset in ("admin.css", "admin.js", "htmx.min.js"):
        assert signed_in.get(f"/admin/static/{asset}").status_code == 200
    assert signed_in.get("/admin/offers/" + "a" * 64).status_code == 404
    assert signed_in.get("/admin/research?key=missing").status_code == 404


def test_research_distinguishes_asking_unconfirmed_and_future(signed_in, seeded):
    from app.schemas.deals import AvailableObservation
    from app.schemas.pricing import PriceType

    base = seeded.request.observations[0]
    observations = list(seeded.request.observations)
    for external, price_type, confirmed, available in (
        ("DEMO-asking-only", PriceType.ASKING, False, seeded.request.as_of),
        ("DEMO-unconfirmed", PriceType.SOLD, False, seeded.request.as_of),
        ("DEMO-future-hidden", PriceType.SOLD, True, seeded.request.as_of + timedelta(days=3)),
    ):
        observation = base.observation.model_copy(
            update={
                "external_id": external,
                "price_type": price_type,
                "sale_confirmed": confirmed,
                "features": base.observation.features.model_copy(update={"product_name": external}),
            }
        )
        observations.append(AvailableObservation(observation=observation, available_at=available))
    request = seeded.request.model_copy(
        update={
            "observations": tuple(observations),
            "as_of": seeded.request.as_of + timedelta(days=1),
        }
    )
    DealPipeline(signed_in.app.state.runtime.analysis_store).process(request)
    offer = request.snapshot.offer
    response = signed_in.get(
        "/admin/research", params={"key": offer_key(offer.source_name, offer.external_id)}
    )
    assert response.status_code == 200
    assert "ASKING — ofertowa" in response.text and "Niepotwierdzona transakcja" in response.text
    assert "DEMO-future-hidden" not in response.text
