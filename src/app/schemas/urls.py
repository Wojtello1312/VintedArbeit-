"""Validated source-provided links; never fetched or inferred."""

from urllib.parse import urlsplit


def validate_listing_url(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 1500 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("invalid listing URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("listing URL must use HTTPS and contain no credentials")
    # Resolve parsing errors (for instance an invalid port) without fetching the URL.
    _ = parsed.port
    return value
