"""HTML formatting of deterministic facts; listing text can never inject Telegram HTML."""

import hashlib
import json
from html import escape

from app.schemas.deals import AnalysisRecord, DealState
from app.schemas.pricing import FrozenSchema
from app.schemas.urls import validate_listing_url


class AlertMessage(FrozenSchema):
    alert_key: str
    analysis_id: str
    source_name: str
    external_id: str
    is_demo: bool
    destination: str
    html: str


def destination_key(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode()).hexdigest()


def make_alert(record: AnalysisRecord, destination: str) -> AlertMessage:
    if record.state != DealState.APPROVED:
        raise ValueError("only approved deals can be queued")
    snapshot = record.request.snapshot
    offer = snapshot.offer
    estimate, profit, score, risk = record.estimate, record.profit, record.score, record.risk
    if (
        estimate is None
        or profit is None
        or score is None
        or risk is None
        or estimate.estimated_price is None
        or profit.estimated_profit is None
        or risk.requires_review
        or profit.unknown_costs
    ):
        raise ValueError("alert requires complete approved deterministic facts")
    url = validate_listing_url(snapshot.listing_url)
    if url is None:
        raise ValueError("listing URL is required; links are never guessed")
    destination = "demo" if offer.is_demo else destination
    if not destination:
        raise ValueError("recipient is not configured")
    identity = json.dumps([offer.source_name, offer.external_id, offer.is_demo, destination])
    key = hashlib.sha256(identity.encode()).hexdigest()
    # Bound each untrusted field before escaping, never truncate HTML markup.
    name = escape(offer.product_name[:180])
    reasons = "; ".join(escape(reason.code[:60]) for reason in record.reasons[:4])
    sources = ", ".join(escape(source[:60]) for source in estimate.sources[:3])
    discount = "nieznany" if profit.market_discount is None else f"{profit.market_discount:.1%}"
    ai = "pominięte"
    if record.ai_review is not None:
        ai = f"{record.ai_review.status}: {record.ai_review.reason}"
    title = "DEMO — fikcyjna okazja" if offer.is_demo else "Vinted Deal Hunter — okazja"
    text = (
        f"<b>{title}</b>\n<b>{name}</b>\n"
        f'<a href="{escape(url, quote=True)}">Zobacz ofertę</a>\n'
        f"Zakup: <b>{offer.asking_price} {offer.currency}</b>\n"
        f"Cena referencyjna: {estimate.estimated_price} {estimate.currency} "
        f"({estimate.price_basis.value})\n"
        f"Konserwatywna odsprzedaż: {profit.conservative_resale_price} {profit.currency}\n"
        f"Koszty dodatkowe: {profit.total_additional_costs} {profit.currency}\n"
        f"Szacunkowy zysk: <b>{profit.estimated_profit} {profit.currency}</b>\n"
        f"Scoring: {score.total:.2f}/100 ({escape(score.version[:80])})\n"
        f"Confidence wyceny: {estimate.confidence.value} "
        f"({estimate.confidence_score:.0%}), próba: {estimate.sample_size}\n"
        f"Źródła: {sources}\n"
        f"Risk level: <b>{risk.level.value}</b>\n"
        f"Kwalifikacja: filtry i próg scoringu spełnione; rabat {discount}; "
        f"jawne koszty; {reasons}\n"
        f"AI: {escape(ai[:160])}\n"
        "Estymacja nie gwarantuje sprzedaży ani autentyczności."
    )
    if len(text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("message exceeds Telegram's limit")
    return AlertMessage(
        alert_key=key,
        analysis_id=record.analysis_id,
        source_name=offer.source_name,
        external_id=offer.external_id,
        is_demo=offer.is_demo,
        destination=destination,
        html=text,
    )
