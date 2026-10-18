# Vinted Deal Hunter

Obecny zakres: offline data ingestion, Product Matcher, Market Pricing
i deterministyczny Deal Engine z opcjonalnym AI Reviewerem, schedulerem, alertami Telegram i polskim panelem administracyjnym.
Import obsługuje CSV, JSON i mock, normalizację, deduplikację
SQLite, raport, CLI oraz `POST /imports`. Wycena wykorzystuje RapidFuzz, medianę,
IQR i jawne dowody ASKING/SOLD. AI i Telegram są domyślnie wyłączone.
Scheduler działa offline na MockOfferSource. Nie ma połączeń z Vinted ani scrapera.

Metodologia, konfiguracja i uruchamialny przykład wyceny:
[docs/pricing.md](docs/pricing.md). Moduły `app/core` i `app/schemas` znajdują
się pod `src/app/`; pakiet importu pozostaje w `src/vinted_deal_hunter/`.

Filtry okazji, jawne koszty, scoring, ryzyko, checkpointy i historyczny backtest:
[docs/deal_engine.md](docs/deal_engine.md). Uruchomienie demo:
`uv run --frozen python backtest.py`. Silnik nie wywołuje AI ani nie wysyła alertów.

Opcjonalny reviewer używa OpenAI Responses API i Structured Outputs:
[docs/ai_reviewer.md](docs/ai_reviewer.md). Włączenie wymaga `VDH_AI`, klucza
`OPENAI_API_KEY` i jawnych stawek modelu; `OPENAI_MODEL` domyślnie wskazuje
`gpt-5-nano`. Brak klucza lub budżetu pozostawia decyzję deterministyczną.
Import CLI/API i historyczny backtest nadal nie wykonują wywołań AI.

ETAP 6 — trwały outbox, asynchroniczny Telegram, limity, deduplikacja oraz APScheduler:
[docs/alerting_scheduler.md](docs/alerting_scheduler.md). Sterowanie przez
`GET /jobs/status` i `POST /jobs/run`, `/jobs/pause`, `/jobs/resume` wymaga
`Authorization: Bearer <JOBS_API_TOKEN>` (minimum 32 znaki). Brak tokenu blokuje
sterowanie. Konfiguracja środowiska: [.env.example](.env.example).

ETAP 7 — dashboard, oferty, okazje, historia cen, ustawienia i monitoring:
[docs/admin.md](docs/admin.md). Panel pod `/admin` wymaga `ADMIN_PASSWORD_HASH`
generowanego przez `uv run --frozen python -m app.admin.password`. Sesje i CSRF
chronią formularze; HTMX i Tailwind są dostarczane lokalnie.

## ETAP 8 — uruchomienie i weryfikacja

Dockerfile, Compose, healthcheck, wersjonowane migracje Alembic oraz pełne demo
100 ofert są dostępne. Instrukcje dla **Windows PowerShell i Docker Compose**:
[docs/deployment.md](docs/deployment.md). API i autoryzacja:
[docs/api.md](docs/api.md). Wyniki audytu i ograniczenia:
[docs/readiness.md](docs/readiness.md).

Szybkie uruchomienie po skonfigurowaniu hasha administratora i tokenu API:

```sh
docker compose up -d --build --wait
docker compose exec app python -m app.demo
```

Panel działa pod `/admin`; healthcheck: `/health/ready`. Zwykłe `docker compose down`
zachowuje dane w wolumenie. API importu, odczytu ofert i jobów wymaga teraz
Bearer `JOBS_API_TOKEN`. Nie ma połączenia z Vinted. Demo i testy są offline.

## Instalacja i kontrole

Wymagania: Python 3.12+ oraz `uv`. Polecenia wykonuj w katalogu repozytorium:

```sh
uv sync --locked
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src
```

Jeżeli domyślna lokalizacja cache jest tylko do odczytu, ustaw
`UV_CACHE_DIR=/tmp/vdh-uv-cache`. Po instalacji uruchamianie oraz testy nie
wymagają sieci ani kluczy API. Zależności są zapisane w `uv.lock`.

## CLI

```sh
uv run --frozen deal-hunter
uv run --frozen deal-hunter --format csv --file src/vinted_deal_hunter/data/demo_offers.csv
uv run --frozen deal-hunter --format json --file src/vinted_deal_hunter/data/demo_offers.json
```

### Ręczny test zapisanej oferty HTML

Aplikacja nie pobiera stron Vinted i nie automatyzuje przeglądarki. Pojedynczą
ofertę, którą użytkownik otworzył zgodnie ze swoimi uprawnieniami, można zapisać
w przeglądarce jako plik HTML i zaimportować lokalnie:

```powershell
uv run --frozen deal-hunter --format html --file .\oferta.html `
  --source-name vinted-manual --no-is-demo
```

Parser nie wykonuje JavaScriptu ani żądań sieciowych. Odczytuje wyłącznie
JSON-LD typu `Product`/`Offer`, a w drugiej kolejności metadane Open Graph.
Jeżeli zapisana strona nie zawiera ceny, waluty, nazwy i stabilnego
identyfikatora, import kończy się jawnym błędem. Nieznany stan produktu jest
oznaczany jako `unknown`. Import tego samego pliku ponownie jest deduplikowany.
To narzędzie służy do ręcznej walidacji; nie jest crawlerem wyszukiwarki.

Domyślnie źródło to `demo`, dane są oznaczone `is_demo=true`, a baza powstaje
w `.data/offers.sqlite3`. `--db /ścieżka/do/bazy.sqlite3` wybiera inną bazę.
Równoważny punkt wejścia: `python -m vinted_deal_hunter` w aktywnym środowisku.
Mock ma stałą nazwę `demo` i nie może działać jako źródło produkcyjne.

Każdy plik demo zawiera te same 130 rekordów: 110 poprawnych unikalnych ofert,
10 duplikatów i 10 rekordów celowo błędnych. Wszystkie produkty są fikcyjne,
ich nazwy i identyfikatory zawierają DEMO. Pierwszy import do pustej bazy daje
`new=110, skipped=10, errors=10`; drugi: `new=0, skipped=120, errors=10`.
CSV i JSON ze wspólną nazwą źródła deduplikują się także między formatami.

CLI wypisuje raport JSON na stdout, logi na stderr. Kody wyjścia:
`0` — import bez błędnych rekordów (także same duplikaty), `1` — import
z błędnymi rekordami, `2` — błąd konfiguracji, pliku, struktury lub bazy.
Przykład demo celowo zwraca `1`; nie oznacza to awarii importera.

Własne autoryzowane eksporty wymagają stabilnej nazwy, np.
`--source-name authorized-export --no-is-demo`. Flaga jest deklaracją pochodzenia,
nie dowodem uprawnień. Nie oznaczaj fixture demo jako danych rzeczywistych.

## API

```sh
uv run --frozen uvicorn vinted_deal_hunter.api:create_app --factory --host 127.0.0.1 --port 8000
```

W drugim terminalu:

```sh
curl --fail-with-body -X POST 'http://127.0.0.1:8000/imports?format=json&source_name=demo&is_demo=true' \
  -H "Authorization: Bearer $JOBS_API_TOKEN" \
  -H 'Content-Type: application/json' --data-binary @src/vinted_deal_hunter/data/demo_offers.json
curl --fail-with-body -X POST 'http://127.0.0.1:8000/imports?format=csv&source_name=demo&is_demo=true' \
  -H "Authorization: Bearer $JOBS_API_TOKEN" \
  -H 'Content-Type: text/csv' --data-binary @src/vinted_deal_hunter/data/demo_offers.csv
curl --fail-with-body -X POST \
  'http://127.0.0.1:8000/imports?format=html&source_name=vinted-manual&is_demo=false' \
  -H "Authorization: Bearer $JOBS_API_TOKEN" \
  -H 'Content-Type: text/html' --data-binary @oferta.html
```

Body zawiera cały plik, bez multipart i bez ścieżki do pliku na serwerze.
Parametry domyślne: `format=json`, `source_name=demo`, `is_demo=true`.
Odpowiedź `200` zawiera raport również przy błędach pojedynczych rekordów.
`413` oznacza przekroczenie limitu bajtów, `415` niewłaściwy Content-Type,
`422` błędny format/strukturę lub limit rekordów, `503` problem z bazą.
Przy awarii dokumentu lub transakcji żadne oferty z tego importu nie są zapisane.
API jest chronione Bearer `JOBS_API_TOKEN`; brak/błędne poświadczenia dają 401,
a brak skonfigurowanego tokenu — 503. Pełny opis: [docs/api.md](docs/api.md).

## Kontrakt wejścia i normalizacja

JSON: tablica obiektów. CSV: UTF-8 (także BOM), separator przecinek, nagłówki
zgodne z nazwami poniżej, standardowe cytowanie CSV. Kwotę z przecinkiem
dziesiętnym trzeba w CSV ująć w cudzysłów. Nie odgadujemy separatora.

| Pole rekordu | Reguły |
| --- | --- |
| `external_id` | Wymagany tekst lub całkowity numer w JSON. Obcięcie skrajnych spacji i NFC; zachowujemy wielkość liter i zera wiodące. |
| `product_name` | Wymagana niepusta nazwa; NFKC i scalenie odstępów. |
| `brand` | Opcjonalna, pusta → null; NFKC, odstępy, casefold i jawne aliasy H&M. Nieznane nazwy pozostają nieznanymi nazwami. |
| `size` | Opcjonalny tekst, pusta → null, NFKC i wielkie litery; bez zgadywania systemu rozmiarów. |
| `condition` | Opcjonalny; `new_with_tags`, `new_without_tags`, `very_good`, `good`, `fair`, `unknown`; jawne polskie odpowiedniki w kodzie. Nieznana etykieta powoduje błąd. |
| `currency` | Wymagany kod ISO, wielkie litery. Obsługiwane PLN, EUR, USD, GBP, CHF, CZK, SEK, DKK, NOK (2 miejsca) oraz JPY (0). Inne kody są odrzucane. |
| `asking_price` | Wymagana dodatnia cena ofertowa, maks. 1 mld; Decimal bez zaokrąglania nadmiarowej precyzji. Tekst z kropką/przecinkiem lub dokładnie parsowana liczba JSON, bez separatorów tysięcy. |
| `published_at` | Opcjonalny ISO-8601 z jawną strefą → UTC; pusta/null → null. Data bez strefy jest błędem, nie zakładamy UTC. |

Nie ma ceny transakcyjnej ani wnioskowania o sprzedaży. Pola `source_name`
i `is_demo` nadaje adapter/kontekst importu i nie mogą być podane w rekordzie.
Nieznane dodatkowe pola są błędem; nie znikają bez raportu.

## Deduplikacja, raport i ograniczenia

Klucz unikalny w SQLite: `(source_name, external_id)`. Nazwy źródeł są
normalizowane do małych liter, dozwolone litery ASCII, cyfry, `_` i `-`.
SQLAlchemy używa atomowego `ON CONFLICT DO NOTHING`, także przy równoległych
importach. Cały zapis odbywa się w jednej transakcji. Kwoty zapisujemy jako
jednostki najmniejsze oraz tekst w payloadzie JSON, bez float; daty w UTC.

Identyczne oferty są pomijane (`duplicate`). Zmienione dane tej samej oferty
też są pomijane, z jawnym powodem `changed_existing`: aktualizacje i historia
obserwacji nie należą do tego etapu. Kolizja oznaczeń demo/rzeczywiste dla tego
samego klucza daje błąd `provenance_conflict`, bez nadpisania oryginału.
Używaj odrębnych nazw źródeł dla eksportów demo i rzeczywistych.

Raport zawiera identyfikator przebiegu, źródło, oznaczenie demo, czasy UTC,
`total`, `new`, `skipped`, `errors`, liczniki powodów pominięcia oraz diagnostykę
rekordów. Zachodzi `total = new + skipped + errors`. `record_number` jest
indeksem rekordu od 1 (bez nagłówka CSV, nie numerem fizycznego wiersza).
Raport podaje pierwszy błąd każdego błędnego rekordu bez całej treści wejściowej.
Po ograniczeniu diagnostyki licznik błędów pozostaje pełny, a
`diagnostics_truncated=true`. Raport nie jest jeszcze utrwalany w bazie.

Konfiguracja środowiskowa (walidowana przy starcie):

| Zmienna | Domyślnie |
| --- | --- |
| `VDH_DB_PATH` | `.data/offers.sqlite3` |
| `VDH_MAX_IMPORT_BYTES` | `2000000` |
| `VDH_MAX_IMPORT_RECORDS` | `10000` |
| `VDH_MAX_REPORT_ERRORS` | `100` |

Pliki są ograniczone rozmiarem i parsowane przed zapisem. Błędny rekord jest
izolowany, ale niepoprawny JSON, uszkodzone cytowanie CSV lub nieprawidłowe
nagłówki to błąd całego dokumentu. Brak danych jest odróżniony od takiej awarii.

Schematy baz są wersjonowane przez Alembic. Polecenie
`uv run --frozen python -m app.migrate` przygotowuje cztery role SQLite i przejmuje
zgodne bazy wcześniejszych etapów bez kasowania danych. Start kontenera wykonuje
migracje automatycznie. Szczegóły backupu: [deployment.md](docs/deployment.md).

`OfferSource` jest protokołem dla adapterów; `AuthorizedOfferSource` to abstrakcyjny
punkt rozszerzenia wymagający udokumentowanej autoryzacji. Nie zawiera klienta
HTTP ani logiki Vinted. Dalsza architektura i plan: [architektura](docs/architecture.md)
oraz [plan](docs/implementation_plan.md). Bieżący stan kolejnych etapów opisują
[wycena](docs/pricing.md) i [Deal Engine](docs/deal_engine.md).
