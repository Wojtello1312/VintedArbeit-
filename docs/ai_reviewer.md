# Opcjonalny AI Reviewer — ETAP 5

Kod znajduje się w `src/app/ai/`. `client.py` używa OpenAI Python SDK
(`responses.parse`, Responses API, Structured Outputs z `AIReview`),
`prompts.py` buduje wersjonowane instrukcje i JSON danych, `reviewer.py`
koordynuje analizę, a `budget.py` przechowuje cache i rezerwacje SQLite.
Core nie korzysta z SDK ani z sieci; otrzymuje walidowany `ReviewOutcome`.
`schemas.py` definiuje odpowiedź i metadane, `config.py` waliduje politykę.

## Uruchomienie i konfiguracja

Domyślnie `enabled=false`, `allow_demo=false`; nie powstaje klient HTTP ani baza
AI. Samo ustawienie klucza nie włącza AI. CLI importu, `POST /imports` i backtest
pozostają offline. Nie dodano automatycznej wysyłki, schedulera ani nowego endpointu.

`AIReviewer.from_env()` odczytuje `VDH_AI` (obiekt JSON), `OPENAI_MODEL`
(domyślnie `gpt-5-nano`) i `OPENAI_API_KEY`. `Settings.from_env().ai` udostępnia
politykę bez klucza. `OPENAI_MODEL` ma pierwszeństwo przed `model` w `VDH_AI`.
Klucz należy dostarczyć przez środowisko/secrets, bez zapisywania w repozytorium.

| Ustawienie w `VDH_AI` | Domyślnie | Znaczenie |
| --- | --- | --- |
| `enabled` / `allow_demo` | `false` / `false` | Oddzielne włączenie AI i przesyłania danych fikcyjnych |
| `db_path` | `.data/ai.sqlite3` | Wspólny trwały ledger i cache dla wszystkich workerów |
| `min_score` | `65` | Nie można skonfigurować wartości niższej niż 65 |
| `min_valuation_confidence` / `min_comparisons` | `0.60` / `5` | Dodatkowa bramka wiarygodnej wyceny |
| `min_review_confidence` / `min_listing_consistency` | `0.60` / `70` | Niższy wynik kieruje do człowieka |
| `daily_budget_usd` / `max_daily_analyses` | `"1"` / `100` | Limity wspólne dla modeli, doba UTC |
| `input_usd_per_million` / `output_usd_per_million` | brak | Wymagane jawne stawki USD za milion tokenów |
| `cached_input_usd_per_million` | brak | Bez stawki cache przyjmujemy pełną stawkę wejściową |
| `timeout_seconds` / `max_attempts` | `20` / `3` | Timeout SDK; maksymalnie 3 próby łącznie |
| `backoff_seconds` / `max_backoff_seconds` | `1` / `10` | Exponential backoff i maksymalny czas oczekiwania |
| `max_input_tokens` / `max_output_tokens` | `32000` / `2000` | Konserwatywna granica wejścia i limit generowania |

Kwoty konfiguruj jako ciągi dziesiętne, nie `float`. Stawki należy dobrać do
`OPENAI_MODEL`, konta i aktualnego [cennika OpenAI](https://openai.com/api/pricing/).
Nie wpisujemy fikcyjnego cennika produkcyjnego. Bez stawek nowe wywołania mają
status `skipped`, powód `pricing_unconfigured`. Zmiana modelu wymaga sprawdzenia
stawek i kompatybilności Responses API/Structured Outputs. Brak kompatybilności
kończy się trybem deterministic-only. SDK używa standardowej usługi
`service_tier=default`, nie włącza narzędzi ani dodatkowo płatnych funkcji.

Aby dołączyć reviewera do przygotowanego `AnalysisRequest`:

```python
from pathlib import Path

from app.ai.reviewer import AIReviewer, ReviewedDealPipeline
from app.core.deal_pipeline import DealPipeline
from app.deal_config import DealConfig
from app.storage.sqlite import SQLiteAnalysisStore
from vinted_deal_hunter.config import Settings

settings = Settings.from_env()
policy = DealConfig.model_validate({**settings.deal.model_dump(), "ai_review_required": True})
store = SQLiteAnalysisStore(Path(".data/deals.sqlite3"))
reviewer = AIReviewer.from_env()
service = ReviewedDealPipeline(DealPipeline(store, policy, settings.pricing), reviewer)
try:
    # request: AnalysisRequest ze snapshotem i uprawnionymi obserwacjami.
    result = service.process(request)
    # service.process_batch(requests) izoluje błędy poszczególnych analiz.
finally:
    reviewer.close()
    store.close()
```

`ai_review_required=true` oznacza skierowanie do opcjonalnego etapu, a nie
wymuszenie dostępności zewnętrznej usługi. Sam `DealPipeline` nie wywołuje AI;
przy tej fladze należy użyć `ReviewedDealPipeline`, aby zakończyć AI_PENDING.

## Bramki i decyzje

Do API trafia tylko `AI_PENDING`, czyli oferta po deterministycznych filtrach,
wycenie, kosztach, ryzyku i progu zatwierdzenia. Odrzucone oferty, brak danych
oraz oferty wymagające już ręcznej weryfikacji nie trafiają do AI. Próg silnika
wynosi domyślnie 70, więc dodatkowy próg AI 65 nie osłabia wcześniejszego filtra.
Wycena musi mieć status OK, cenę, confidence MEDIUM/HIGH, wynik co najmniej 0.60
oraz co najmniej 5 porównań. Następnie wymagane są zgoda konfiguracji na AI,
klucz, stawki, dopuszczalny rozmiar wejścia i rezerwacja budżetu.

Odpowiedź Pydantic ma dokładnie pola: `listing_consistency` 0–100,
`risk_level` low/medium/high/unknown, `detected_issues`, `product_match_valid`
bool/null, `manual_review_required`, `explanation`, `confidence` 0–1.
Dodatkowe pola (także ceny), niepoprawne typy i NaN są odrzucane. High i unknown,
a także false/null w dopasowaniu modelu, wymuszają `manual_review_required=true`,
nawet jeżeli model zwrócił false. Niska pewność lub spójność także powodują
REVIEW_REQUIRED. Medium podnosi poziom ryzyka; samo w sobie nie wymusza review,
jeżeli model i pozostałe reguły tego nie wymagają.

Wynik AI nie zmienia wyceny, zysku, scoringu ani istniejących flag ryzyka.
Poziom ryzyka można tylko podnieść. Brak ostrzeżeń nigdy nie usuwa znanych
problemów. Pomyślna odpowiedź nie jest dowodem autentyczności. Przy odmowie,
niekompletnej/błędnej odpowiedzi, błędzie usługi lub wyczerpaniu limitu następuje
APPROVED na podstawie spełnionych wcześniej warunków deterministycznych, z jawnym
`ai_review.status=skipped/unavailable` i przyczyną. Nie nazywamy tego oceną AI.
Aktywna analiza innego workera pozostaje AI_PENDING; nie zatwierdzamy jej
przed poznaniem wyniku. Zatwierdzenie nadal nie wykonuje zakupu ani powiadomienia.

## Prompt i granica zaufania

Wersja `listing-review-v1` obejmuje wszystkie siedem aspektów zadania.
Instrukcje systemowe są osobne od JSON ogłoszenia. Payload zawiera metadane,
opis, liczbę zdjęć (bez pobierania zdjęć), wynik deterministyczny i wyłącznie
porównania uwzględnione w wycenie w ich ostatniej dostępnej wersji. Koszty i
surowe dane popytu nie są wysyłane. Każda treść źródła jest niezaufanymi danymi,
także nazwa produktu i referencje dowodowe. Nie ma narzędzi, przeglądania URL,
dostępu modelu do sekretów ani interpretacji ogłoszenia jako instrukcji.
Prompt zabrania wymyślania cen i potwierdzania autentyczności z opisu/zdjęć.

To ogranicza skutki prompt injection, ale nie dowodzi poprawności semantycznej
modelu. Testy weryfikują izolację instrukcji i kontrakt; model nadal może się
mylić, dlatego jego wynik nie zastępuje deterministycznych faktów ani człowieka.
Żądania mają `store=false`; nie stanowi to obietnicy braku retencji po stronie
operatora API. Nie wysyłaj danych, do których przetwarzania nie masz uprawnień.

## Trwałość, cache i koszty

Cache SHA-256 obejmuje kanoniczny payload, nazwę modelu, prompt z wersją oraz
schemat odpowiedzi. Kolejność obserwacji i techniczny `calculated_at` nie
zmieniają hash. Zmiana istotnych danych lub modelu unieważnia cache; zmiana
samego czasu ponownego przeliczenia nie wymaga nowego wywołania. Identyczny
wynik cache można odczytać nawet bez klucza i przy wyczerpanym budżecie.
Ponownie stosujemy aktualne progi pewności/spójności. Cache nie ma TTL: dotyczy
identycznych danych, a aktualność obserwacji sprawdza wcześniej wycena.

SQLite używa transakcji `BEGIN IMMEDIATE` i unikalnego hash. Pierwszy worker
atomowo zajmuje analizę i rezerwuje koszt; pozostali używają cache lub czekają.
`ai_reviews` zapisuje model żądany, prompt, stan i wynik; wynik zawiera także
model zwrócony przez API, czas UTC, tokeny, koszt i rezerwację. `ai_attempts`
zapisuje każdą próbę. Nie zapisujemy kluczy ani surowego payloadu w ledgerze.
Checkpoint analizy utrwala `ReviewOutcome` i powód decyzji. Dwie bazy nie mają
wspólnej transakcji: cache zapisany przed checkpointem pozwala bez nowego
wywołania wznowić analizę po przerwaniu. Awaria zapisu cache nie usuwa już
odebranej informacji o wysokim ryzyku.

Koszty liczymy w Decimal, SQLite przechowuje całkowite nanodolary (10⁻⁹ USD),
zaokrąglane w górę. Przed każdą próbą rezerwujemy pełny limit wejścia i wyjścia
po jawnych stawkach. Wejście ograniczamy konserwatywną liczbą bajtów UTF-8
promptu, JSON i schematu + 2048 zapasu; wyjście ogranicza `max_output_tokens`
API, łącznie z tokenami rozumowania. Nie ucinamy opisu, bo mogłoby to ukryć wadę.
Za duże wejście pomija AI. Znane usage zastępuje rezerwację obliczonym kosztem;
brak usage, timeout i błędna odpowiedź pozostawiają pełną rezerwację, a koszt
pozostaje nieznany. Jest to lokalna kontrola według skonfigurowanych stawek,
nie odczyt rachunku dostawcy. Stawki muszą odpowiadać aktualnej taryfie modelu.

Maksymalna liczba analiz dotyczy unikalnych hash w dobie UTC; retry nie tworzy
nowej analizy, ale każda próba obciąża budżet USD. Retry po północy zalicza się
również do nowej doby. Limity sumują się dla wszystkich modeli korzystających
z tego samego pliku. Osobne kopie bazy nie zapewniają wspólnego limitu.

SDK ma `max_retries=0`; aplikacja ponawia wyłącznie błędy połączenia/timeout,
408/409/429 i 5xx. Opóźnienie rośnie wykładniczo, respektuje `Retry-After`
(sekundy lub data HTTP, także `retry-after-ms`). Jeżeli serwer wymaga oczekiwania
dłuższego niż limit, rezygnujemy z retry zamiast ponawiać za wcześnie.
Nie ponawiamy odmów, niepoprawnego schematu, 401/403 ani innych trwałych błędów.

Po awarii procesu aktywna rezerwacja pozostaje przez okres
`max_attempts × (timeout_seconds + max_backoff_seconds) + 120` sekund.
Po tym czasie wynik staje się niepewny: wracamy do deterministic-only bez
ponownego płatnego zapytania. Nie zwalniamy automatycznie takiej rezerwacji ani
nie deklarujemy exactly-once u dostawcy. Znany błąd również nie jest ponawiany
w nieskończoność przy kolejnych uruchomieniach dla tego samego hash.
Zakończony checkpoint analizy jest niezmienny; nowy punkt czasu/dane tworzą
nową analizę, która może wykorzystać cache. Nie kasuj ledgera, by resetować limit.

## Weryfikacja bez płatnych zapytań

```sh
uv sync --locked
uv run --frozen pytest tests/ai -q
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src backtest.py
uv run --frozen python backtest.py
```

Testy AI blokują rzeczywisty transport HTTP. Testy adaptera używają prawdziwego
SDK z `httpx.MockTransport`; pozostałe korzystają z atrapy protokołu klienta.
Obejmują schemat, bramki i granicę 65, brak klucza, fallback, ryzyko, izolację
promptu, timeout, błędy API, bounded retry, tokeny/koszty, granice budżetu,
race condition, zmianę doby UTC, restart, cache, przerwanie procesu i SQLite.
Nie wykonujemy płatnego testu integracyjnego z OpenAI.
