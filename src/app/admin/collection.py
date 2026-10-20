"""Editable scraper settings. Browser paths and credentials are never form fields."""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from app.schemas.pricing import FrozenSchema
from vinted_deal_hunter.collection_filters import CollectionFilters
from vinted_deal_hunter.vinted import public_url
from vinted_deal_hunter.vinted_playwright import VintedAutomationConfig


class CollectionPolicy(FrozenSchema):
    enabled: bool = False
    search: str = Field(default="", max_length=200)
    catalog_url: str = Field(default="", max_length=2000)
    max_pages: int = Field(default=10, ge=1, le=100)
    browser_channel: Literal["chromium", "msedge", "chrome"] = "msedge"
    filters: CollectionFilters = Field(default_factory=CollectionFilters)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        object.__setattr__(self, "search", " ".join(self.search.split()))
        object.__setattr__(self, "catalog_url", self.catalog_url.strip())
        if self.enabled and bool(self.search) == bool(self.catalog_url):
            raise ValueError("Podaj frazę lub link katalogu")
        if self.catalog_url:
            public_url(self.catalog_url)
            if not urlsplit(self.catalog_url).path.startswith("/catalog"):
                raise ValueError("Wymagany link katalogu")
        return self

    def apply(self, base: VintedAutomationConfig) -> VintedAutomationConfig:
        values = base.model_dump()
        values.update(
            enabled=self.enabled,
            engine="playwright",
            search=self.search or None,
            urls=[self.catalog_url] if self.catalog_url else [],
            filters=self.filters,
        )
        browser = base.browser.model_dump()
        browser.update(
            max_pages=self.max_pages,
            browser_channel=self.browser_channel,
            max_requests=min(250, (self.max_pages + 1) * (base.browser.retries + 1)),
        )
        values["browser"] = browser
        return VintedAutomationConfig.model_validate(values)

    @classmethod
    def from_source(cls, source: VintedAutomationConfig) -> "CollectionPolicy":
        catalog = next(
            (url for url in source.urls if urlsplit(url).path.startswith("/catalog")), ""
        )
        return cls(
            enabled=source.enabled and bool(source.search or catalog),
            search=source.search or "",
            catalog_url=catalog,
            max_pages=source.browser.max_pages,
            browser_channel=source.browser.browser_channel,
            filters=source.filters or CollectionFilters(),
        )
