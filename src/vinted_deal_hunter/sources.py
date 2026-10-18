"""Local adapters and the extension contract for explicitly authorized sources."""

import csv
import io
import json
from abc import ABC, abstractmethod
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Protocol


class SourceError(ValueError):
    """The source could not be read safely; distinct from a bad individual record."""


class OfferSource(Protocol):
    @property
    def source_name(self) -> str: ...

    @property
    def is_demo(self) -> bool: ...

    def read(self, *, max_records: int) -> list[object]: ...


class AuthorizedOfferSource(ABC):
    """Extension only. Implementors must document permission, timeouts and limits.

    No Vinted adapter, networking or implicit authorization is provided here.
    """

    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @property
    def is_demo(self) -> bool:
        return False

    @abstractmethod
    def read(self, *, max_records: int) -> list[object]: ...


def read_local_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SourceError("Cannot read the local import file") from exc
    if len(data) > max_bytes:
        raise SourceError("Import file exceeds the byte limit")
    return data


def decode_content(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("Import requires UTF-8 encoding") from exc


class JSONOfferSource:
    def __init__(self, content: bytes, *, source_name: str, is_demo: bool) -> None:
        self.content = content
        self.source_name = source_name
        self.is_demo = is_demo

    def read(self, *, max_records: int) -> list[object]:
        try:
            data: object = json.loads(decode_content(self.content), parse_float=Decimal)
        except (ValueError, RecursionError) as exc:
            raise SourceError("Invalid JSON document") from exc
        if not isinstance(data, list):
            raise SourceError("JSON document must be an array of offer records")
        if len(data) > max_records:
            raise SourceError("Import exceeds the record limit")
        return data


class CSVOfferSource:
    def __init__(self, content: bytes, *, source_name: str, is_demo: bool) -> None:
        self.content = content
        self.source_name = source_name
        self.is_demo = is_demo

    def read(self, *, max_records: int) -> list[object]:
        reader = csv.DictReader(io.StringIO(decode_content(self.content), newline=""), strict=True)
        records: list[object] = []
        try:
            headers = reader.fieldnames
            required = {"external_id", "product_name", "currency", "asking_price"}
            if not headers or len(set(headers)) != len(headers) or not required <= set(headers):
                raise SourceError("CSV requires unique headers and all required offer columns")
            for record in reader:
                if len(records) >= max_records:
                    raise SourceError("Import exceeds the record limit")
                # A short or overlong row is a bad record, not a source failure.
                if None in record or any(value is None for value in record.values()):
                    records.append(None)
                else:
                    records.append(record)
        except csv.Error as exc:
            raise SourceError("Malformed CSV document") from exc
        return records


class MockOfferSource(JSONOfferSource):
    def __init__(self) -> None:
        super().__init__(
            files("vinted_deal_hunter").joinpath("data/demo_offers.json").read_bytes(),
            source_name="demo",
            is_demo=True,
        )
