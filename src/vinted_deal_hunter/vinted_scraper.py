"""Explicit network CLI: python -m vinted_deal_hunter.vinted_scraper."""

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.sources import SourceError
from vinted_deal_hunter.vinted import VintedOfferSource, VintedScrapeConfig, search_url


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect public Vinted PL asking prices into SQLite"
    )
    seeds = parser.add_mutually_exclusive_group(required=True)
    seeds.add_argument("--url", action="append", help="Public item/catalog URL; may be repeated")
    seeds.add_argument("--search", help="Query for the public catalog HTML")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-requests", type=int, default=25)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    if args.search is not None and not args.search.strip():
        parser.error("--search must contain a nonempty query")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    source: VintedOfferSource | None = None
    repository: SQLiteOfferRepository | None = None
    try:
        settings = Settings.from_env()
        config = VintedScrapeConfig(
            max_pages=args.max_pages,
            max_requests=args.max_requests,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout,
            max_response_bytes=settings.max_import_bytes,
        )
        source = VintedOfferSource([search_url(args.search)] if args.search else args.url, config)
        repository = SQLiteOfferRepository(args.db or settings.db_path)
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
    except (SourceError, ValidationError, OSError, SQLAlchemyError):
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
