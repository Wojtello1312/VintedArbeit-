"""Median/IQR pricing with explicit evidence selection and bounded process-local caching."""

import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from threading import RLock
from typing import Protocol

from app.config import PricingConfig
from app.core.product_matcher import ProductMatcher
from app.schemas.pricing import (
    Confidence,
    EstimateStatus,
    EvidenceReference,
    MarketEstimate,
    ObservationRejection,
    PriceBasis,
    PriceObservation,
    PriceType,
    PricingProduct,
    ProductMatch,
    utc_timestamp,
)
from vinted_deal_hunter.models import CURRENCY_SCALE

logger = logging.getLogger(__name__)


class ObservationSource(Protocol):
    """Adapter must provide provenance and evidence; failure must not mean an empty market."""

    def fetch_comparables(self, product: PricingProduct) -> Sequence[PriceObservation]: ...


@dataclass(frozen=True)
class CacheInfo:
    hits: int
    misses: int
    size: int


def quantile(prices: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """Hyndman–Fan type 7: linear interpolation at (n-1)*p, entirely in Decimal."""
    if not prices or not Decimal(0) <= fraction <= Decimal(1):
        raise ValueError("quantile requires a nonempty sample and fraction in [0, 1]")
    ordered = sorted(prices)
    position = Decimal(len(ordered) - 1) * fraction
    lower = int(position)
    if lower == len(ordered) - 1:
        return ordered[lower]
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * (position - lower)


def now_utc() -> datetime:
    return datetime.now(UTC)


class PriceEstimator:
    def __init__(
        self,
        config: PricingConfig | None = None,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.config = config if config is not None else PricingConfig()
        self.matcher = ProductMatcher(self.config)
        self.clock = clock
        self._cache: OrderedDict[str, MarketEstimate] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0

    def cache_info(self) -> CacheInfo:
        with self._lock:
            return CacheInfo(hits=self._hits, misses=self._misses, size=len(self._cache))

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def estimate_from_source(
        self,
        product: PricingProduct,
        source: ObservationSource,
    ) -> MarketEstimate:
        # Fetch on every request: this cache must not conceal a changed dataset or source outage.
        return self.estimate(product, source.fetch_comparables(product))

    def estimate(
        self,
        product: PricingProduct,
        observations: Iterable[PriceObservation],
    ) -> MarketEstimate:
        snapshots = tuple(observations)
        with self._lock:
            now = utc_timestamp(self.clock())
            key = self._cache_key(product, snapshots, now)
            cached = self._cache.get(key)
            if cached is not None and (
                timedelta(0)
                <= now - cached.calculated_at
                < timedelta(seconds=self.config.cache_ttl_seconds)
            ):
                self._hits += 1
                self._cache.move_to_end(key)
                return cached.model_copy(update={"product_id": product.product_id})
            self._misses += 1
            result = self._calculate(product, snapshots, now)
            if self.config.cache_ttl_seconds:
                self._cache[key] = result
                self._cache.move_to_end(key)
                while len(self._cache) > self.config.cache_max_entries:
                    self._cache.popitem(last=False)
            logger.info(
                "Pricing status=%s basis=%s samples=%d confidence=%s",
                result.status,
                result.price_basis,
                result.sample_size,
                result.confidence,
            )
            return result

    def _cache_key(
        self,
        product: PricingProduct,
        observations: tuple[PriceObservation, ...],
        now: datetime,
    ) -> str:
        max_age = timedelta(days=self.config.max_observation_age_days)
        data = {
            "product": product.model_dump(mode="json", exclude={"product_id"}),
            "config": self.config.model_dump(mode="json"),
            "observations": sorted(
                (
                    observation.model_dump_json(),
                    timedelta(0) <= now - observation.observed_at <= max_age,
                )
                for observation in observations
            ),
        }
        # Include age eligibility so a cache hit cannot keep a newly stale/fresh observation.
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _calculate(
        self,
        product: PricingProduct,
        observations: tuple[PriceObservation, ...],
        now: datetime,
    ) -> MarketEstimate:
        rejections: list[ObservationRejection] = []

        def reject(
            observation: PriceObservation,
            reasons: tuple[str, ...],
            match: ProductMatch | None = None,
        ) -> None:
            rejections.append(
                ObservationRejection(
                    source_name=observation.source_name,
                    external_id=observation.external_id,
                    reasons=reasons,
                    product_match_score=None if match is None else match.product_match_score,
                )
            )

        # One listing is one comparable, regardless of how many times it was observed.
        groups: dict[tuple[str, str], list[PriceObservation]] = {}
        for observation in observations:
            groups.setdefault((observation.source_name, observation.external_id), []).append(
                observation
            )
        eligible: list[tuple[PriceObservation, ProductMatch]] = []
        for identity, group in sorted(groups.items()):
            observation = group[0]
            if identity == (product.source_name, product.external_id):
                reject(observation, ("self_observation",))
                continue
            distinct = {item.model_dump_json() for item in group}
            if len(distinct) > 1:
                reject(observation, ("conflicting_observations",))
                continue
            if len(group) > 1:
                reject(observation, ("duplicate_observation",))
            reasons: list[str] = []
            if observation.currency != product.currency:
                reasons.append("currency_mismatch")
            if observation.is_demo != product.is_demo:
                reasons.append("provenance_mismatch")
            if observation.source_reliability < self.config.min_source_reliability:
                reasons.append("unreliable_source")
            if observation.observed_at > now:
                reasons.append("future_observation")
            elif now - observation.observed_at > timedelta(
                days=self.config.max_observation_age_days
            ):
                reasons.append("stale_observation")
            if observation.price_type == PriceType.SOLD and (
                not observation.sale_confirmed or observation.evidence_reference is None
            ):
                reasons.append("unconfirmed_sale")
            match = self.matcher.match(product.features, observation.features)
            reasons.extend(match.rejection_reasons)
            if reasons:
                reject(observation, tuple(reasons), match)
            else:
                eligible.append((observation, match))

        sold = [item for item in eligible if item[0].price_type == PriceType.SOLD]
        selected = sold if sold else eligible
        basis = (
            (PriceBasis.SOLD if sold else PriceBasis.ASKING_BASED) if selected else PriceBasis.NONE
        )
        if sold:
            for observation, match in eligible:
                if observation.price_type == PriceType.ASKING:
                    reject(observation, ("asking_not_used_when_sold_available",), match)

        kept = selected
        outlier_count = 0
        if len(selected) >= self.config.min_observations:
            prices = [observation.price for observation, _ in selected]
            q1, q3 = quantile(prices, Decimal("0.25")), quantile(prices, Decimal("0.75"))
            spread = q3 - q1
            factor = Decimal(str(self.config.iqr_multiplier))
            lower, upper = q1 - factor * spread, q3 + factor * spread
            kept = []
            for observation, match in selected:
                if lower <= observation.price <= upper:
                    kept.append((observation, match))
                else:
                    outlier_count += 1
                    reject(observation, ("iqr_outlier",), match)

        evidence = tuple(
            EvidenceReference(
                source_name=observation.source_name,
                external_id=observation.external_id,
                observed_at=observation.observed_at,
                evidence_reference=observation.evidence_reference,
            )
            for observation, _ in kept
        )
        common = {
            "product_id": product.product_id,
            "is_demo": product.is_demo,
            "currency": product.currency,
            "price_basis": basis,
            "sample_size": len(kept),
            "calculated_at": now,
            "sources": tuple(sorted({item.source_name for item in evidence})),
            "included_observations": evidence,
            "rejections": tuple(rejections),
            "outlier_count": outlier_count,
        }
        if len(kept) < self.config.min_observations:
            reason = "sample_below_minimum_after_iqr" if outlier_count else "sample_below_minimum"
            return MarketEstimate.model_validate(
                {
                    **common,
                    "status": EstimateStatus.INSUFFICIENT_DATA,
                    "estimated_price": None,
                    "median": None,
                    "q1": None,
                    "q3": None,
                    "confidence": Confidence.NONE,
                    "confidence_score": 0,
                    "confidence_reasons": (reason,),
                }
            )

        prices = [observation.price for observation, _ in kept]
        median = quantile(prices, Decimal("0.5"))
        q1, q3 = quantile(prices, Decimal("0.25")), quantile(prices, Decimal("0.75"))
        confidence, score, confidence_reasons = self._confidence(kept, basis, median, q3 - q1)
        quantum = Decimal(1).scaleb(-CURRENCY_SCALE[product.currency])
        return MarketEstimate.model_validate(
            {
                **common,
                "status": EstimateStatus.OK,
                "estimated_price": median.quantize(quantum, rounding=ROUND_HALF_UP),
                "median": median,
                "q1": q1,
                "q3": q3,
                "confidence": confidence,
                "confidence_score": score,
                "confidence_reasons": confidence_reasons,
            }
        )

    def _confidence(
        self,
        observations: list[tuple[PriceObservation, ProductMatch]],
        basis: PriceBasis,
        median: Decimal,
        iqr: Decimal,
    ) -> tuple[Confidence, float, tuple[str, ...]]:
        size = len(observations)
        components = {
            "sample": min(1.0, size / self.config.confidence_full_sample_size),
            "match": sum(match.product_match_score for _, match in observations) / size / 100,
            "reliability": sum(item.source_reliability for item, _ in observations) / size,
            "dispersion": max(0.0, 1.0 - float(iqr / median)),
        }
        weights = self.config.confidence_weights.model_dump()
        score = sum(weights[key] * value for key, value in components.items()) / sum(
            weights.values()
        )
        reasons: list[str] = []
        if basis == PriceBasis.ASKING_BASED:
            score = min(score, self.config.asking_confidence_cap)
            reasons.append("asking_prices_are_not_transactions")
        if any("model_unverified" in match.warnings for _, match in observations):
            score = min(score, self.config.missing_model_confidence_cap)
            reasons.append("model_unverified")
        if any("condition_differs" in match.warnings for _, match in observations):
            reasons.append("mixed_conditions_allowed_by_configuration")
        score = round(max(0.0, min(1.0, score)), 4)
        confidence = Confidence.LOW
        if score >= self.config.confidence_high_threshold:
            confidence = Confidence.HIGH
        elif score >= self.config.confidence_medium_threshold:
            confidence = Confidence.MEDIUM
        return confidence, score, tuple(reasons)
