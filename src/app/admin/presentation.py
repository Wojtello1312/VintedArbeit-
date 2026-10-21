"""Polish presentation of stable domain codes without changing stored evidence."""

from datetime import UTC, datetime

from app.schemas.deals import DecisionReason
from vinted_deal_hunter.sources import SourceErrorCode

SOURCE_ERRORS = {
    SourceErrorCode.PLAYWRIGHT_NOT_INSTALLED: (
        "Brak biblioteki Playwright. Zatrzymaj serwer, wykonaj "
        "uv sync --locked --extra browser i uruchom serwer ponownie z --extra browser."
    ),
    SourceErrorCode.CHROMIUM_NOT_INSTALLED: (
        "Brak przeglądarki Chromium wymaganej przez Playwright. "
        "Wybierz zainstalowany Microsoft Edge w konfiguracji pobierania i zapisz warunki. "
        "Możesz też zainstalować Chromium poleceniem: "
        "uv run --frozen --extra browser playwright install chromium."
    ),
    SourceErrorCode.BROWSER_NOT_INSTALLED: (
        "Nie znaleziono wybranej przeglądarki. Zainstaluj ją lub wybierz "
        "zainstalowaną przeglądarkę w konfiguracji pobierania i zapisz warunki."
    ),
    SourceErrorCode.BROWSER_START_FAILED: (
        "Nie udało się uruchomić przeglądarki. Sprawdź jej instalację, "
        "wybór w konfiguracji pobierania i uprawnienia do uruchamiania programów."
    ),
    SourceErrorCode.BROWSER_SUBPROCESS_UNSUPPORTED: (
        "Środowisko Python nie pozwala Playwright uruchomić procesu przeglądarki. "
        "Na Windows uruchom aplikację w zwykłym terminalu PowerShell; "
        "pętla asyncio używana przez Playwright musi obsługiwać podprocesy (Proactor)."
    ),
}


def source_error_text(code: SourceErrorCode | None) -> str:
    if code is not None:
        return SOURCE_ERRORS[code]
    return "Sprawdź konfigurację źródła. Ten cykl nie zawiera szczegółowego kodu przyczyny."


REASONS = {
    "policy_passed": "Spełniono wszystkie deterministyczne kryteria kwalifikacji.",
    "retry": "Wznowiono analizę od ostatniego zapisanego etapu.",
    "currency_mismatch": "Waluta oferty, polityki i kosztów musi być zgodna.",
    "brand_unknown": "Brak wymaganej marki produktu.",
    "brand_excluded": "Marka jest poza listą obserwowanych marek.",
    "category_unknown": "Brak wymaganej kategorii produktu.",
    "category_excluded": "Kategoria jest poza listą dozwolonych kategorii.",
    "price_below_minimum": "Cena zakupu jest poniżej ustawionego minimum.",
    "price_above_maximum": "Cena zakupu przekracza ustawione maksimum.",
    "condition_unknown": "Stan produktu jest nieznany.",
    "condition_excluded": "Stan produktu nie jest dozwolony.",
    "excluded_keyword": "Ogłoszenie zawiera wykluczone słowo lub frazę.",
    "insufficient_comparisons": "Zbyt mało wiarygodnych obserwacji po filtrowaniu.",
    "match_score_unknown": "Nie można potwierdzić minimalnego dopasowania produktu.",
    "match_score_below_minimum": "Dopasowanie produktu jest poniżej minimum.",
    "comparisons_unknown": "Nieznana liczba porównań cenowych.",
    "comparisons_below_minimum": "Liczba porównań jest poniżej minimum.",
    "profit_unknown": "Brak danych do potwierdzenia minimalnego zysku.",
    "profit_below_minimum": "Przewidywany zysk jest poniżej minimum.",
    "market_discount_unknown": "Nieznany rabat względem ceny referencyjnej.",
    "market_discount_below_minimum": "Rabat względem ceny referencyjnej jest zbyt mały.",
    "score_below_minimum": "Scoring jest poniżej progu kwalifikacji.",
    "ai_review_pending": "Oczekiwanie na opcjonalną weryfikację AI.",
    "ai_review_findings": "Weryfikacja AI wskazuje ryzyko; sprawdź zapisany wynik.",
    "delivery_confirmed": "Zapisano potwierdzenie dostarczenia powiadomienia.",
    "unknown_costs": "Nieznane koszty uniemożliwiają pełne wyliczenie zysku.",
    "asking_based": "Wycena bazuje na cenach ofertowych, bez potwierdzonych transakcji.",
    "low_valuation_confidence": "Wiarygodność wyceny jest poniżej minimum.",
    "unknown_demand": "Brak aktualnych, dostępnych dowodów popytu.",
    "incomplete_listing": "Kompletność ogłoszenia jest poniżej minimum.",
    "predicted_loss": "Znane koszty wskazują przewidywaną stratę.",
}


def reason_text(reason: DecisionReason) -> str:
    return REASONS.get(reason.code, reason.message)


def utc_display(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M:%S")
