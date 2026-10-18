"""Cookie sessions and CSRF checks for HTML forms, including login/logout."""

import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request

from app.admin.store import AdminStore, digest
from vinted_deal_hunter.config import Settings

SESSION_COOKIE = "vdh_admin_session"
LOGIN_COOKIE = "vdh_login_csrf"


def credential_fingerprint(settings: Settings) -> str:
    if settings.admin_password_hash is None:
        raise HTTPException(503, "Panel nie jest skonfigurowany. Ustaw ADMIN_PASSWORD_HASH.")
    return digest(settings.admin.username + "\0" + settings.admin_password_hash.get_secret_value())


async def form_data(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Wymagany formularz HTML.")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 32768:
            raise HTTPException(413, "Formularz jest zbyt duży.")
    try:
        values = parse_qs(content.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Niepoprawny formularz.") from exc
    if any(len(value) != 1 for value in values.values()):
        raise HTTPException(400, "Powtórzone pole formularza.")
    return {key: value[0] for key, value in values.items()}


def check_csrf(request: Request, form: dict[str, str], expected: str | None) -> None:
    origin = request.headers.get("origin")
    trusted = urlsplit(str(request.base_url))
    if origin is not None:
        parsed = urlsplit(origin)
        if (parsed.scheme, parsed.netloc) != (trusted.scheme, trusted.netloc):
            raise HTTPException(403, "Niedozwolone pochodzenie formularza.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Niedozwolone pochodzenie formularza.")
    supplied = form.get("csrf", "")
    if (
        not expected
        or not supplied
        or not secrets.compare_digest(supplied.encode(), expected.encode())
    ):
        raise HTTPException(403, "Niepoprawny token CSRF. Odśwież formularz.")


def authenticated(request: Request, settings: Settings, store: AdminStore) -> str:
    fingerprint = credential_fingerprint(settings)
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf = store.session(token, datetime.now(UTC), fingerprint) if token else None
    if csrf is None:
        # Redirect HTMX at top level instead of inserting a login page into a table.
        raise HTTPException(
            401 if request.headers.get("HX-Request") == "true" else 303,
            headers={"Location": "/admin/login", "HX-Redirect": "/admin/login"},
        )
    return csrf
