import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import import_offers
from vinted_deal_hunter.sources import (
    CSVOfferSource,
    JSONOfferSource,
    MockOfferSource,
    OfferSource,
    SourceError,
    read_local_file,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline offer import; default: bundled demo data")
    parser.add_argument("--format", choices=["csv", "json", "mock"], default="mock")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--source-name", default="demo")
    parser.add_argument("--is-demo", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    if args.format != "mock" and args.file is None:
        parser.error("--file is required for CSV/JSON imports")
    if args.format == "mock" and (args.file is not None or not args.is_demo):
        parser.error("mock imports cannot use --file or --no-is-demo")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repository: SQLiteOfferRepository | None = None
    try:
        settings = Settings.from_env()
        if args.db is not None:
            settings = Settings.model_validate({**settings.model_dump(), "db_path": args.db})
        source: OfferSource
        if args.format == "mock":
            source = MockOfferSource()
        else:
            content = read_local_file(args.file, max_bytes=settings.max_import_bytes)
            adapter = CSVOfferSource if args.format == "csv" else JSONOfferSource
            source = adapter(content, source_name=args.source_name, is_demo=args.is_demo)
        repository = SQLiteOfferRepository(settings.db_path)
        report = import_offers(source, repository, settings)
        print(report.model_dump_json(indent=2))
        return 1 if report.errors else 0
    except (SourceError, OSError, SQLAlchemyError):
        logging.error("Import failed: unreadable source, invalid document or unavailable storage")
        return 2
    except ValidationError:
        logging.error("Invalid environment configuration")
        return 2
    finally:
        if repository is not None:
            repository.close()
