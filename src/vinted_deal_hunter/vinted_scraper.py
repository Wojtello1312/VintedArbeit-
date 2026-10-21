"""Explicit network CLI: python -m vinted_deal_hunter.vinted_scraper."""

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

import portalocker
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import PartialImportError, import_offers
from vinted_deal_hunter.sources import SourceError
from vinted_deal_hunter.vinted import VintedOfferSource, VintedScrapeConfig, search_url
from vinted_deal_hunter.vinted_playwright import (
    VintedPlaywrightConfig,
    VintedPlaywrightOfferSource,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect public Vinted PL asking prices into SQLite"
    )
    seeds = parser.add_mutually_exclusive_group(required=True)
    seeds.add_argument("--url", action="append", help="Public item/catalog URL; may be repeated")
    seeds.add_argument("--search", help="Query for the public catalog HTML")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument(
        "--max-requests",
        type=int,
        help="Request budget including robots and retries (default: derived from --max-pages)",
    )
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=10_000_000,
        help="Maximum rendered HTML size per page (default: 10000000)",
    )
    parser.add_argument("--engine", choices=["http", "playwright"], default="playwright")
    parser.add_argument(
        "--headed", action="store_true", help="Show Chromium; does not bypass access challenges"
    )
    parser.add_argument("--scroll-rounds", type=int, default=3)
    parser.add_argument(
        "--browser-channel", choices=["chromium", "chrome", "msedge"], default="chromium"
    )
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    if args.search is not None and not args.search.strip():
        parser.error("--search must contain a nonempty query")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    source: VintedOfferSource | VintedPlaywrightOfferSource | None = None
    repository: SQLiteOfferRepository | None = None
    try:
        settings = Settings.from_env()
        urls = [search_url(args.search)] if args.search else args.url
        common = {
            "max_pages": args.max_pages,
            "max_requests": (
                args.max_requests
                if args.max_requests is not None
                else min(250, 1 + args.max_pages * (VintedScrapeConfig().retries + 1))
            ),
            "interval_seconds": args.interval,
            "timeout_seconds": args.timeout,
            "max_response_bytes": args.max_response_bytes,
        }
        if args.engine == "playwright":
            browser_config = VintedPlaywrightConfig(
                **common,
                headless=not args.headed,
                scroll_rounds=args.scroll_rounds,
                browser_channel=args.browser_channel,
                browser_executable=args.browser_executable,
            )
            source = VintedPlaywrightOfferSource(urls, browser_config)
        else:
            if (
                args.headed
                or args.scroll_rounds != 3
                or args.browser_channel != "chromium"
                or args.browser_executable is not None
            ):
                parser.error("browser options require --engine playwright")
            source = VintedOfferSource(urls, VintedScrapeConfig(**common))
        db_path = args.db or settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(db_path) + ".vinted.lock", mode="a", timeout=0):
            repository = SQLiteOfferRepository(db_path)
            imported = import_offers(source, repository, settings)
        print(
            json.dumps(
                {
                    "collection": source.report.model_dump(mode="json"),
                    "import": imported.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if imported.errors or any(p.status == "error" for p in source.report.pages) else 0
    except PartialImportError as exc:
        logging.error(
            "Collection interrupted code=%s; preserved new=%d skipped=%d",
            exc.code or "unknown",
            exc.report.new,
            exc.report.skipped,
        )
        print(
            json.dumps(
                {
                    "collection": source.report.model_dump(mode="json") if source else None,
                    "import": exc.report.model_dump(mode="json"),
                    "error": "collection_interrupted",
                    "error_code": exc.code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    except (
        SourceError,
        ValidationError,
        OSError,
        SQLAlchemyError,
        portalocker.exceptions.LockException,
    ):
        logging.error("Collection/import failed; inspect the collection report when available")
        print(
            json.dumps(
                {
                    "collection": source.report.model_dump(mode="json") if source else None,
                    "import": None,
                    "error": "collection_or_import_failed",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    finally:
        if repository is not None:
            repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
