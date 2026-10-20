"""Server-rendered administration with progressive HTMX enhancement."""

import asyncio
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypedDict, cast
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.responses import Response

from app.admin.collection import CollectionPolicy
from app.admin.password import verify_password
from app.admin.policy import PanelPolicy
from app.admin.presentation import reason_text, utc_display
from app.admin.queries import AdminQueries, OfferFilters, OfferView
from app.admin.security import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    authenticated,
    check_csrf,
    credential_fingerprint,
    form_data,
)
from app.admin.store import Feedback, RevisionConflict, digest
from app.core.deal_pipeline import visible_observations
from app.scheduler.runtime import JobRuntime
from app.scheduler.service import JobBusy
from app.schemas.pricing import PriceObservation, PriceType
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.models import CURRENCY_SCALE


class ResearchRow(TypedDict):
    value: PriceObservation
    confirmed: bool
    included: bool


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
STATE_LABELS = {
    "NEW": "Nowa",
    "NORMALIZED": "Znormalizowana",
    "FILTERED": "Po filtrach",
    "MATCHED": "Dopasowana",
    "VALUED": "Wyceniona",
    "SCORED": "Oceniona",
    "AI_PENDING": "Oczekuje na AI",
    "APPROVED": "Okazja",
    "REJECTED": "Odrzucona",
    "REVIEW_REQUIRED": "Do weryfikacji",
    "NOTIFIED": "Powiadomiono",
    "INSUFFICIENT_DATA": "Za mało danych",
    "ERROR": "Błąd",
}
RISK_LABELS = {"LOW": "Niskie", "MEDIUM": "Średnie", "HIGH": "Wysokie"}
FEEDBACK_LABELS = {
    "good_deal": "Dobra okazja",
    "bad_deal": "Słaba okazja",
    "purchased": "Kupione",
    "ignored": "Ignorowane",
}
CONDITION_LABELS = {
    "new_with_tags": "Nowy z metką",
    "new_without_tags": "Nowy bez metki",
    "very_good": "Bardzo dobry",
    "good": "Dobry",
    "fair": "Zadowalający",
    "unknown": "Nieznany",
}
TEMPLATES.env.globals.update(
    states=STATE_LABELS,
    risks=RISK_LABELS,
    labels=FEEDBACK_LABELS,
    reason_text=reason_text,
    conditions=CONDITION_LABELS,
)
TEMPLATES.env.filters["utc_display"] = utc_display


def admin_router(config: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin", include_in_schema=False)

    def runtime(request: Request) -> JobRuntime:
        return cast(JobRuntime, request.app.state.runtime)

    def render(request: Request, name: str, csrf: str = "", **values: object) -> Response:
        return TEMPLATES.TemplateResponse(
            request=request, name=name, context={"csrf": csrf, "path": request.url.path, **values}
        )

    def identity(request: Request) -> tuple[JobRuntime, str]:
        rt = runtime(request)
        return rt, authenticated(request, config, rt.panel)

    def require_offer(queries: AdminQueries, key: str) -> OfferView:
        if len(key) != 64:
            raise HTTPException(404, "Nie znaleziono oferty.")
        offer = queries.offer(key)
        if offer is None:
            raise HTTPException(404, "Nie znaleziono oferty.")
        return offer

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        credential_fingerprint(config)
        csrf = secrets.token_urlsafe(32)
        response = render(request, "login.html", csrf)
        response.set_cookie(
            LOGIN_COOKIE,
            csrf,
            httponly=True,
            samesite="strict",
            secure=config.admin.secure_cookies,
            max_age=600,
            path="/admin",
        )
        return response

    @router.post("/login")
    async def login(request: Request) -> Response:
        fingerprint = credential_fingerprint(config)
        data = await form_data(request)
        check_csrf(request, data, request.cookies.get(LOGIN_COOKIE))
        rt = runtime(request)
        bucket = digest(request.client.host if request.client else "local")
        allowed = await asyncio.to_thread(
            rt.panel.allow_login,
            bucket,
            datetime.now(UTC),
            config.admin.login_attempts,
            config.admin.login_window_seconds,
        )
        if not allowed:
            raise HTTPException(
                429,
                "Zbyt wiele prób. Spróbuj ponownie później.",
                headers={"Retry-After": str(config.admin.login_window_seconds)},
            )
        password, username = data.get("password", ""), data.get("username", "")
        if len(password) > 1024 or len(username) > 100:
            raise HTTPException(400, "Niepoprawne dane logowania.")
        assert config.admin_password_hash is not None
        valid = await asyncio.to_thread(
            verify_password, password, config.admin_password_hash.get_secret_value()
        )
        if not valid or not secrets.compare_digest(
            username.encode(), config.admin.username.encode()
        ):
            response = render(
                request, "login.html", data.get("csrf", ""), error="Niepoprawny login lub hasło."
            )
            response.status_code = 401
            return response
        old_token = request.cookies.get(SESSION_COOKIE)
        if old_token:
            rt.panel.logout(old_token)
        token, _ = rt.panel.new_session(
            datetime.now(UTC), config.admin.session_seconds, fingerprint
        )
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=config.admin.secure_cookies,
            max_age=config.admin.session_seconds,
            path="/admin",
        )
        response.delete_cookie(LOGIN_COOKIE, path="/admin")
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        rt, csrf = identity(request)
        check_csrf(request, await form_data(request), csrf)
        rt.panel.logout(request.cookies.get(SESSION_COOKIE, ""))
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/admin")
        return response

    @router.get("", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        rt, csrf = identity(request)
        queries = AdminQueries(rt.settings)
        stats = await asyncio.to_thread(queries.dashboard, production=True)
        offers, _ = await asyncio.to_thread(
            queries.offers, OfferFilters(sort="score", mode="production"), deals=True
        )
        return render(
            request,
            "dashboard.html",
            csrf,
            title="Przegląd",
            stats=stats,
            offers=offers[:5],
            status=await rt.manager.status(),
        )

    async def listings(request: Request, filters: OfferFilters, deals: bool) -> Response:
        rt, csrf = identity(request)
        updates = {}
        if "mode" not in request.query_params:
            updates["mode"] = "production"
        if "currency" not in request.query_params and not any(
            value is not None
            for value in (filters.min_price, filters.max_price, filters.min_profit)
        ):
            updates["currency"] = "all"
        filters = filters.model_copy(update=updates)
        if filters.currency == "all" and any(
            value is not None
            for value in (filters.min_price, filters.max_price, filters.min_profit)
        ):
            raise HTTPException(422, "Wybierz jedną walutę dla filtrów ceny i zysku.")
        offers, total = await asyncio.to_thread(
            AdminQueries(rt.settings).offers, filters, deals=deals
        )
        values = filters.model_dump(mode="json", exclude_none=True)
        pages = max(1, (total + 24) // 25)
        previous = "?" + urlencode({**values, "page": max(1, filters.page - 1)})
        following = "?" + urlencode({**values, "page": filters.page + 1})
        template = (
            "offer_table.html" if request.headers.get("HX-Request") == "true" else "offers.html"
        )
        return render(
            request,
            template,
            csrf,
            title="Okazje" if deals else "Oferty",
            offers=offers,
            total=total,
            filters=filters,
            pages=pages,
            previous=previous,
            following=following,
            deals=deals,
            currencies=CURRENCY_SCALE,
            demo_count=AdminQueries(rt.settings).dashboard()["demo_count"],
        )

    @router.get("/offers", response_class=HTMLResponse)
    async def offers(request: Request, filters: Annotated[OfferFilters, Query()]) -> Response:
        return await listings(request, filters, False)

    @router.get("/deals", response_class=HTMLResponse)
    async def deals(request: Request, filters: Annotated[OfferFilters, Query()]) -> Response:
        if "sort" not in request.query_params:
            filters = filters.model_copy(update={"sort": "score"})
        return await listings(request, filters, True)

    @router.get("/offers/{key}", response_class=HTMLResponse)
    async def detail(
        request: Request, key: str, page: int = Query(default=1, ge=1, le=100000)
    ) -> Response:
        rt, csrf = identity(request)
        queries = AdminQueries(rt.settings)
        offer = require_offer(queries, key)
        history, total = queries.history(offer.offer, page)
        feedback = rt.panel.feedback_history(offer.offer.source_name, offer.offer.external_id)
        return render(
            request,
            "detail.html",
            csrf,
            title="Szczegóły oferty",
            item=offer,
            history=history,
            total=total,
            page=page,
            feedback=feedback,
        )

    @router.post("/offers/{key}/feedback")
    async def feedback(request: Request, key: str) -> Response:
        rt, csrf = identity(request)
        data = await form_data(request)
        check_csrf(request, data, csrf)
        offer = require_offer(AdminQueries(rt.settings), key)
        label = data.get("label", "")
        if label not in FEEDBACK_LABELS:
            raise HTTPException(422, "Niepoprawna ocena.")
        rt.panel.feedback(
            offer.offer.source_name,
            offer.offer.external_id,
            cast(Feedback, label),
            datetime.now(UTC),
        )
        return RedirectResponse(f"/admin/offers/{key}", status_code=303)

    @router.get("/research", response_class=HTMLResponse)
    async def research(
        request: Request,
        key: str | None = None,
        q: Annotated[str, Query(max_length=200)] = "",
        currency: Annotated[
            str, Query(pattern="^(PLN|EUR|USD|GBP|CHF|CZK|SEK|DKK|NOK|JPY)$")
        ] = "PLN",
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
    ) -> Response:
        rt, csrf = identity(request)
        filters = OfferFilters(q=q, currency=currency, page=page, mode="production")
        queries = AdminQueries(rt.settings)
        selected = require_offer(queries, key) if key else None
        rows, total = queries.offers(filters)
        history, history_total = (
            queries.history(selected.offer, filters.page) if selected else ([], 0)
        )
        observations: list[ResearchRow] = []
        seen: set[str] = set()
        if selected and selected.analysis:
            analysis = selected.analysis
            visible = {item.model_dump_json() for item in visible_observations(analysis.request)}
            for item in analysis.request.observations:
                observation = item.observation
                if (
                    item.available_at > analysis.request.as_of
                    or observation.observed_at > analysis.request.as_of
                ):
                    continue
                serialized = observation.model_dump_json()
                if serialized in seen:
                    continue
                seen.add(serialized)
                confirmed = (
                    observation.price_type == PriceType.SOLD
                    and observation.sale_confirmed
                    and bool(observation.evidence_reference)
                )
                included = (
                    serialized in visible
                    and any(
                        ref.source_name == observation.source_name
                        and ref.external_id == observation.external_id
                        and ref.observed_at == observation.observed_at
                        for ref in analysis.estimate.included_observations
                    )
                    if analysis.estimate
                    else False
                )
                observations.append(
                    {"value": observation, "confirmed": confirmed, "included": included}
                )
            observations.sort(key=lambda row: row["value"].observed_at, reverse=True)
        return render(
            request,
            "research.html",
            csrf,
            title="Badanie cen",
            item=selected,
            offers=rows,
            total=total,
            filters=filters,
            history=history,
            history_total=history_total,
            observations=observations[:200],
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        rt, csrf = identity(request)
        revision, saved = rt.panel.policy()
        return render(
            request,
            "settings.html",
            csrf,
            title="Ustawienia",
            revision=revision,
            policy=saved or PanelPolicy.from_settings(rt.settings),
            currency=rt.settings.deal.currency,
            saved=request.query_params.get("saved") == "1",
        )

    @router.post("/settings")
    async def update_settings(request: Request) -> Response:
        rt, csrf = identity(request)
        data = await form_data(request)
        check_csrf(request, data, csrf)
        try:
            revision = int(data.pop("revision", "-1"))
            data.pop("csrf", None)
            payload: dict[str, object] = dict(data)
            saved_policy = rt.panel.policy()[1]
            payload["collection"] = saved_policy.collection if saved_policy else None
            payload["watched_brands"] = tuple(
                value.strip()
                for value in data.get("watched_brands", "").split(",")
                if value.strip()
            )
            for field in ("scheduler_enabled", "ai_enabled"):
                payload[field] = data.get(field) == "on"
            policy = PanelPolicy.model_validate(payload)
        except (ValueError, ValidationError):
            # Keep all displayed settings sourced from validated policy, not submitted secrets.
            response = render(
                request,
                "settings.html",
                csrf,
                title="Ustawienia",
                revision=rt.panel.policy()[0],
                policy=rt.panel.policy()[1] or PanelPolicy.from_settings(rt.settings),
                currency=rt.settings.deal.currency,
                error="Sprawdź zakresy liczb, marki i limity cen.",
            )
            response.status_code = 422
            return response
        lease = rt.manager.store.acquire()
        if lease is None:
            raise HTTPException(409, "Trwa cykl. Zapisz ustawienia po jego zakończeniu.")
        try:
            rt.panel.save_policy(policy, revision, datetime.now(UTC))
            rt.refresh_policy()
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            lease.close()
        return RedirectResponse("/admin/settings?saved=1", status_code=303)

    @router.get("/system", response_class=HTMLResponse)
    async def system(request: Request) -> Response:
        rt, csrf = identity(request)
        details = await asyncio.to_thread(AdminQueries(rt.settings).system)
        return render(
            request,
            "system.html",
            csrf,
            title="System",
            status=await rt.manager.status(),
            revision=rt.panel.policy()[0],
            collection=CollectionPolicy.from_source(rt.settings.vinted),
            currencies=CURRENCY_SCALE,
            saved=request.query_params.get("saved") == "1",
            **details,
        )

    @router.get("/jobs/progress", response_class=HTMLResponse)
    async def progress(request: Request) -> Response:
        rt, csrf = identity(request)
        return render(request, "cycle_progress.html", csrf, status=await rt.manager.status())

    @router.post("/collection")
    async def collection_settings(request: Request) -> Response:
        rt, csrf = identity(request)
        data = await form_data(request)
        check_csrf(request, data, csrf)
        try:
            revision = int(data.pop("revision", "-1"))
            data.pop("csrf", None)
            filters = {
                key: data.pop(key, "")
                for key in (
                    "currency",
                    "min_price",
                    "max_price",
                    "condition",
                    "category",
                    "brand",
                    "size",
                )
            }
            policy = CollectionPolicy.model_validate(
                {**data, "enabled": data.get("enabled") == "on", "filters": filters}
            )
        except (ValueError, ValidationError):
            raise HTTPException(
                422, "Sprawdź frazę lub link katalogu, zakres cen, walutę i limit stron."
            ) from None
        lease = rt.manager.store.acquire()
        if lease is None:
            raise HTTPException(409, "Trwa cykl. Zapisz ustawienia po jego zakończeniu.")
        try:
            _, saved_policy = rt.panel.policy()
            saved_policy = saved_policy or PanelPolicy.from_settings(rt.settings)
            rt.panel.save_policy(
                saved_policy.model_copy(update={"collection": policy}), revision, datetime.now(UTC)
            )
            rt.refresh_policy()
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            lease.close()
        return RedirectResponse("/admin/system?saved=1", status_code=303)

    @router.post("/offers/{key}/delete")
    async def delete_offer(request: Request, key: str) -> Response:
        rt, csrf = identity(request)
        check_csrf(request, await form_data(request), csrf)
        lease = rt.manager.store.acquire()
        if lease is None:
            raise HTTPException(409, "Poczekaj na zakończenie cyklu przed usunięciem ofert.")
        try:
            repo = cast(SQLiteOfferRepository, request.app.state.repository)
            outbox = rt.manager.cycle.notifications.outbox
            if key == "demo":
                await asyncio.to_thread(outbox.cancel_demo)
                await asyncio.to_thread(repo.delete_demo)
            else:
                item = require_offer(AdminQueries(rt.settings), key)
                await asyncio.to_thread(
                    outbox.cancel_offer, item.offer.source_name, item.offer.external_id
                )
                await asyncio.to_thread(
                    repo.delete_offer, item.offer.source_name, item.offer.external_id
                )
        finally:
            lease.close()
        return RedirectResponse("/admin/offers", status_code=303)

    @router.post("/jobs/{action}")
    async def control(request: Request, action: Literal["run", "pause", "resume"]) -> Response:
        rt, csrf = identity(request)
        check_csrf(request, await form_data(request), csrf)
        try:
            if action == "run":
                await rt.manager.submit()
            elif action == "pause":
                await rt.manager.pause()
            else:
                await rt.manager.resume()
        except JobBusy as exc:
            raise HTTPException(409, "Cykl już trwa.") from exc
        if request.headers.get("HX-Request") == "true":
            response = render(
                request, "cycle_progress.html", csrf, status=await rt.manager.status()
            )
            response.status_code = 202 if action == "run" else 200
            return response
        return RedirectResponse("/admin/system", status_code=303)

    return router
