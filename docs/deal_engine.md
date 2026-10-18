# Deterministic Deal Engine — etap 4

Moduły `src/app/core/filters.py`, `scoring.py`, `risk_engine.py` i
`deal_pipeline.py` realizują deterministyczną analizę. Core używa interfejsu
`AnalysisStore`, bez zależności od ORM lub FastAPI. Adaptery są w `app/storage/`:
`MemoryAnalysisStore` dla backtestu i `SQLiteAnalysisStore` dla trwałych analiz.
Nie ma wywołań AI, zakupów ani wysyłki wiadomości.

## Dane, filtry i koszty

`DealSnapshot` obejmuje istniejący `OfferInput`, jawnie podany model, kategorię,
opis, liczbę zdjęć, `CostBreakdown`, opcjonalne dowody popytu i `available_at`.
To czas, kiedy **cała ta wersja danych** była znana. Nie wolno dopisywać do starego
snapshotu później poznanych kosztów lub cech. Brak modelu pozostaje nieznany;
nie jest wyciągany z tytułu. Brak informacji nie jest zamieniany na dobry stan,
wysoki popyt ani zero kosztów.

Filtry wstępne sprawdzają markę, kategorię, walutę, cenę, stan i wykluczone
słowa/frazy (bez dopasowywania fragmentów innych słów). Końce przedziału ceny
są dozwolone. Puste listy marek/kategorii oznaczają brak ograniczenia, a brak
wymaganej marki/kategorii kieruje do przeglądu. Nieznany stan również wymaga
przeglądu. Jawne naruszenie reguły ma pierwszeństwo i daje odrzucenie.

Minimalny match jest sprawdzany przy doborze porównań i dla faktycznie użytej
próby. Stosujemy ostrzejszy próg spośród `DealConfig` i `PricingConfig`.
Minimum porównań dotyczy próbki po IQR i musi spełniać oba zestawy wymagań.
Progi minimalnego zysku, rabatu i score są domknięte: równość przechodzi filtr.

Cena konserwatywna = `min(estimated_price, q1) × (1 − resale_haircut)`, domyślnie
z 10% redukcją, zaokrąglona w dół do skali waluty. Potencjalny zysk to:

```text
conservative_resale_price − purchase_price − buyer_fees − inbound_shipping
− resale_fees − outbound_shipping_subsidy − other_costs
```

Każdy koszt jest jawny, nieujemny i w tej samej walucie. `null` oznacza nieznany
koszt, natomiast `"0.00"` — jawnie podane zero. Choćby jeden nieznany koszt
powoduje `estimated_profit=null`, `total_additional_costs=null`, listę
`unknown_costs` i blokadę zatwierdzenia. Nie wyznaczamy automatycznie prowizji.
Zysk może być ujemny. Rabat rynkowy to `(estimated_price − purchase_price) /
estimated_price`; uwzględnienie kosztów następuje w zysku. Cały rachunek używa
Decimal, bez zaokrąglania score przed porównaniem z progami.

## Score i ryzyko

`deal-score-v1` używa poniższych komponentów 0–100 i wag sumujących się do 100.

| Komponent | Waga | Obliczenie |
| --- | --- | --- |
| Rabat | 30 | `100 × rabat / discount_full_score`, ograniczone do 0–100 |
| Zysk | 25 | `100 × zysk / profit_full_score`, ograniczone do 0–100 |
| Confidence | 20 | `100 × confidence_score` wyceny |
| Stan | 10 | Nowy z metką 100, bez metki 90, bardzo dobry 80, dobry 60, zadowalający 30 |
| Popyt | 10 | `100 × score` z jawnego, aktualnego dowodu |
| Kompletność | 5 | Udział znanych pól w stałym zestawie ośmiu pól |

Kompletność uwzględnia markę, model, rozmiar, znany stan, kategorię, opis,
co najmniej jedno zdjęcie i datę publikacji. Liczba zdjęć i obecność opisu nie
stanowią dowodu autentyczności. Brak komponentu daje 0 punktów, a jego waga
nie jest rozdzielana na inne komponenty. Wynik zawiera komponenty, wkłady
ważone, braki danych i wersję algorytmu.

Dowód popytu wymaga źródła, score 0–1, czasu obserwacji i dostępności. Nieznany,
przestarzały albo jeszcze niedostępny dowód daje 0 punktów. Jawnie potwierdzony
popyt równy zero pozostaje znaną wartością. Domyślnie nieznany popyt jest flagą
ryzyka, lecz sam nie blokuje zatwierdzenia; `require_known_demand` może to zmienić.

Ryzyko jest jakościowe, nie jest prawdopodobieństwem oszustwa. Nieznane koszty,
niska wiarygodność i niewystarczająca kompletność wymagają przeglądu. Wycena
ASKING domyślnie również wymaga przeglądu. Ujemny zysk daje wysokie ryzyko i
narusza minimalny zysk. Każda flaga zawiera kod, opis, poziom i informację,
czy blokuje automatyczne zatwierdzenie.

## Stany i wznowienia

```text
NEW → NORMALIZED → FILTERED → MATCHED → VALUED → SCORED
SCORED → APPROVED / REJECTED / REVIEW_REQUIRED / AI_PENDING
APPROVED → NOTIFIED (wyłącznie po dostarczeniu potwierdzenia)
```

Naruszenia filtrów mogą kończyć analizę po FILTERED. Zbyt mała próba po VALUED
daje INSUFFICIENT_DATA bez score. Po SCORED pierwszeństwo mają: jawne naruszenia
filtrów, wymagany przegląd, minimalny score, opcjonalna weryfikacja i zatwierdzenie.
Sam `DealPipeline.process()` pozostawia AI_PENDING jako stan oczekiwania i nie
wywołuje AI. W ETAPIE 5 `ReviewedDealPipeline` dodaje opcjonalną weryfikację,
a `complete_ai_review()` atomowo zapisuje APPROVED albo REVIEW_REQUIRED.
Opis: [ai_reviewer.md](ai_reviewer.md). APPROVED nie oznacza dokonania zakupu
ani wysłania alertu.

Checkpoint zawiera wejście, politykę, wersję, historię przejść, przyczyny decyzji,
dopasowania, wycenę, koszty, score i ryzyko. Identyfikator analizy jest skrótem
snapshotu, obserwacji, `as_of`, konfiguracji i wersji algorytmów. Powtórzenie
tego samego zlecenia, również z inną kolejnością obserwacji, zwraca ten sam zapis.
Zmiana danych, polityki albo czasu wyceny tworzy nową analizę i zachowuje poprzednią.

Adapter SQLite zatwierdza każdy etap atomowo z kontrolą numeru rewizji
(`compare_and_set`). Równoległe procesy mogą powtórzyć czyste obliczenie,
ale tylko jedno przejście danej rewizji jest zapisane. Przerwanie procesu nie
pozostawia blokady wymagającej ręcznego zdjęcia. Ponowne `process(request)`
wznawia od ostatniego zapisanego etapu.

Błąd obliczeń zapisuje ERROR i `resume_state`. Ponowna próba wznawia ten etap;
domyślnie dopuszczamy 3 próby. Historia błędów pozostaje w zapisie, bez pełnych
tekstów wyjątków mogących zawierać dane wejściowe. Awaria magazynu nie jest
przedstawiana jako zapisany ERROR: `process()` zgłasza błąd, a `process_batch()`
raportuje indeks w `failures` i kontynuuje pozostałe zlecenia. Zwrócone rekordy
mogą osobno zawierać stan ERROR. Dane wejściowe są walidowane przez Pydantic.

`mark_notified(analysis_id, receipt)` tylko rejestruje potwierdzenie dostarczenia;
nie wysyła wiadomości. Wymaga APPROVED, znanego identyfikatora dostarczenia
i czasu między zatwierdzeniem a chwilą bieżącą. Powtórzenie tego samego
potwierdzenia jest idempotentne. Dla demo dozwolony jest wyłącznie kanał `demo`.
Przyszły adapter powiadomień musi zapewnić outbox i deduplikację ofert/kanałów
między różnymi wersjami analizy; obecny etap nie implementuje wysyłki.

## Konfiguracja i uruchomienie

`Settings.deal` odczytuje JSON z `VDH_DEAL`; wszystkie progi i wagi są w
`app/deal_config.py`. Niepoprawna suma wag lub zakres odrzuca konfigurację.

| Parametr | Domyślnie |
| --- | --- |
| `currency`, `min_price`, `max_price` | PLN; 0; 1000 |
| `allowed_brands`, `allowed_categories`, `excluded_keywords` | puste listy |
| `allowed_conditions` | nowy z metką/bez, bardzo dobry, dobry |
| `min_match_score`, `min_comparisons` | 80; 5 |
| `min_profit`, `min_market_discount` | 20 jednostek waluty; 0.20 |
| `resale_haircut`, `discount_full_score`, `profit_full_score` | 0.10; 0.50; 100 |
| `approval_score`, `min_valuation_confidence`, `min_completeness` | 70; 0.60; 0.50 |
| `demand_max_age_days`, `max_attempts` | 30; 3 |
| `review_asking_based`, `require_known_demand`, `ai_review_required` | true; false; false |
| `weights`, `condition_scores` | tabele powyżej |

Przykład: `VDH_DEAL='{"min_profit":"30","approval_score":"75"}'`.
Progi kwotowe odnoszą się do `currency`; nie przeliczamy walut.

Przykład trwałego przetwarzania pojedynczego snapshotu demo (uruchom jako
plik przez `uv run --frozen python plik.py` po `uv sync --locked`):

```python
from importlib.resources import files
from pathlib import Path

from app.backtest import HistoricalDemo
from app.core.deal_pipeline import DealPipeline
from app.schemas.deals import AnalysisRequest
from app.storage.sqlite import SQLiteAnalysisStore
from vinted_deal_hunter.config import Settings

dataset = HistoricalDemo.model_validate_json(
    files("app").joinpath("data/backtest_demo.json").read_bytes()
)
snapshot = dataset.snapshots[0]
request = AnalysisRequest(
    snapshot=snapshot, as_of=snapshot.available_at, observations=dataset.observations
)
settings = Settings.from_env()
store = SQLiteAnalysisStore(Path(".data/deals.sqlite3"))
try:
    pipeline = DealPipeline(store, settings.deal, settings.pricing)
    result = pipeline.process_batch([request, request])
    print([record.state.value for record in result.records], result.failures)
finally:
    store.close()
```

Domyślnie otrzymasz dwa odwołania do tej samej zatwierdzonej analizy i brak
awarii. Adapter tworzy osobną tabelę `deal_analyses`; przykład używa osobnego
pliku bazy. Nie modyfikuje tabel importu. Przyszłe zmiany schematu checkpointów
wymagają migracji; nie dodano jeszcze migracji Alembic ani endpointu analiz.

## Historyczny backtest bez look-ahead bias

```sh
uv run --frozen python backtest.py
uv run --frozen python backtest.py --data src/app/data/backtest_demo.json
uv run --frozen pytest tests/deals
```

Skrypt zwraca JSON. Kod wyjścia 0 oznacza ukończony przebieg (także z poprawnymi
odrzuceniami/brakami danych), 1 obecność ERROR w analizach, a 2 błąd danych
lub niemożność zakończenia przebiegu. Dane domyślne to 12 fikcyjnych ofert,
57 obserwacji cen i 5 oddzielnych wyników sprzedaży; wszystkie oznaczone demo.
Zbiór zawiera opóźnione dowody, przyszłe ceny i nieznane koszty/popyt.

Zlecenia są uporządkowane według `available_at` snapshotów. W chwili decyzji
widoczne są wyłącznie obserwacje z `observed_at <= as_of` **oraz**
`available_at <= as_of`. Późniejsza korekta wcześniejszego zdarzenia nie jest
widoczna wcześniej; dla danego ID wybieramy najnowszą dostępną wersję, a
sprzeczne wersje z tym samym czasem dostępności odrzuca estymator. Jego zegar
działa na historycznym `as_of`, nie na dacie uruchomienia backtestu.

Wyniki późniejszej sprzedaży nie są przekazywane do pipeline. Dopiero po
ustaleniu wszystkich decyzji służą do ewaluacji, jeśli były znane do
`evaluation_at`. Brak wyniku sprzedaży albo nieznane koszty rzeczywiste pozostają
nieznane; nie oznaczają zera zysku ani braku sprzedaży. Agregat zysku dotyczy
wyłącznie zatwierdzonych ofert ze znanym wynikiem. Nie ma strojenia progów na
późniejszych wynikach. Każda oferta docelowa występuje w backteście raz.

Wynik domyślny: 4 APPROVED, 3 REJECTED, 3 REVIEW_REQUIRED, 2 INSUFFICIENT_DATA.
Z 4 zatwierdzonych ofert 3 mają znany wynik: suma fikcyjnych zysków wynosi
462 PLN, jedna pozostaje nieoceniona. To sprawdzenie mechaniki na danych
syntetycznych, a nie pomiar skuteczności na rzeczywistym rynku.
