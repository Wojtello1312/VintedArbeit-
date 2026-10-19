# Alerting i scheduler — ETAP 6

`src/app/notifications/` zawiera formatowanie HTML, asynchronicznego klienta
Telegram, trwały outbox i serwis wysyłki. `src/app/scheduler/` łączy źródło,
import, kontekst analizy, pipeline, kolejkę oraz APScheduler 3.x. Core nadal
nie zależy od klienta Telegram ani APScheduler.

## Domyślne działanie i start

Po starcie API harmonogram uruchamia cykl co 300 sekund. Pierwsze automatyczne
wykonanie następuje po upływie interwału. Bez adaptera produkcyjnego używany
jest `MockOfferSource`. Importowane dane są jawnie demonstracyjne; scheduler
nie wywołuje dla nich AI ani Telegram, nawet gdy skonfigurowano klucze.
Nie opróżnia wtedy również starych alertów produkcyjnych pozostawionych w kolejce.

Mock zawiera 130 wierszy: 110 unikalnych poprawnych ofert, 10 duplikatów i 10
celowo błędnych rekordów. Cykl analizuje każdą poprawną tożsamość raz. Brak
porównań, kosztów i linków pozostaje brakiem danych: domyślny cykl nie produkuje
fikcyjnych zysków ani alertów. Pełne testy kwalifikacji korzystają z jawnych fixture
oraz lokalnego odbiornika demo. Zmiana danych tej samej tożsamości jest ponownie
analizowana; sprzeczne rekordy jednej tożsamości w tym samym imporcie są pomijane
w analizie i liczone jako błędy.

```sh
uv sync --locked
cp .env.example .env
# Uzupełnij JOBS_API_TOKEN prywatnie: losowy token o długości co najmniej 32 znaków.
# TELEGRAM_BOT_TOKEN i CHAT_ID nie są potrzebne do pracy offline.
set -a
. ./.env
set +a
uv run --frozen uvicorn vinted_deal_hunter.api:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1
```

`.env` jest ignorowany przez Git. Aplikacja czyta środowisko procesu; sama nie
ładuje `.env`. Użyj menedżera sekretów przy wdrożeniu. Nigdy nie umieszczaj
sekretów w query string, repozytorium ani logach. Szablon nie zawiera prawdziwych
danych dostępowych. Zachowano lokalny charakter API; wystawienie poza localhost
wymaga HTTPS/reverse proxy i odpowiedniej ochrony całego API. Ochrona opisana
poniżej obejmuje `/jobs/*`; istniejący `/imports` pozostaje lokalnym endpointem
bez uwierzytelniania.

## Endpointy

Wszystkie cztery endpointy wymagają `Authorization: Bearer <JOBS_API_TOKEN>`.
Sekret jest porównywany w stałym czasie. Brak skonfigurowanego tokenu oznacza
503 i blokadę dostępu. Brak/niepoprawne poświadczenia oznaczają 401.

| Endpoint | Działanie |
| --- | --- |
| `GET /jobs/status` | Stan harmonogramu, pauza, blokada cyklu, następny tick UTC, źródło, ostatni raport, liczniki oraz stan outboxa i limitu dobowego |
| `POST /jobs/run` | Wykonuje jeden cykl i zwraca jego raport; opcjonalny JSON `{"run_id":"moj-cykl"}` |
| `POST /jobs/pause` | Trwale wstrzymuje automatyczne cykle; nie przerywa już trwającego |
| `POST /jobs/resume` | Wznawia automatyczne cykle na kolejnych tickach interwału |

```sh
curl --fail-with-body -H "Authorization: Bearer $JOBS_API_TOKEN" \
  http://127.0.0.1:8000/jobs/status
curl --fail-with-body -X POST -H "Authorization: Bearer $JOBS_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"run_id":"manual-001"}' \
  http://127.0.0.1:8000/jobs/run
curl --fail-with-body -X POST -H "Authorization: Bearer $JOBS_API_TOKEN" \
  http://127.0.0.1:8000/jobs/pause
curl --fail-with-body -X POST -H "Authorization: Bearer $JOBS_API_TOKEN" \
  http://127.0.0.1:8000/jobs/resume
```

`run_id` może mieć 1–128 znaków ASCII: litery, cyfry, `_`, `-`. Powtórzenie
zakończonego identyfikatora zwraca ten sam zapisany raport, również po restarcie,
bez ponownego importu i wysyłki. Dotyczy to także raportu zakończonego błędem.
Aby wykonać nowy cykl, pomiń ID albo wybierz nowe. Równoległy cykl zwraca 409.
Pauza dotyczy automatycznego harmonogramu: ręczny `/run` pozostaje dostępny.
Wyłączenie `scheduler.enabled` również nie wyłącza ręcznego sterowania.

`/run` czeka asynchronicznie na zakończenie i zwraca 200 z raportem. Sprawdzaj
jego `status`, `import_errors` i `analysis_errors`, nie tylko kod HTTP. Anulowanie
żądania przez klienta nie przerywa cyklu ani nie zwalnia blokady przed końcem
pracy. Przy zamykaniu aplikacji czekamy na aktywny cykl. Raport zawiera czas
wykonania, liczbę wczytanych, nowych, pominiętych, błędnych i przeanalizowanych
ofert, rozkład decyzji oraz wyniki kolejki/wysyłki. Błędy logujemy nazwą typu,
bez treści odpowiedzi zewnętrznych i sekretów.

## Konfiguracja

| Zmienna | Domyślnie / znaczenie |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Brak; token bota w formacie `numer:sekret` |
| `CHAT_ID` | Brak; liczbowy identyfikator (także ujemny) lub `@nazwa_kanalu` |
| `JOBS_API_TOKEN` | Brak; osobny sekret sterowania, minimum 32 znaki |
| `VDH_SCHEDULER` | JSON polityki harmonogramu |
| `VDH_NOTIFICATIONS` | JSON polityki alertów |

`VDH_SCHEDULER`: `enabled=true`, `interval_seconds=300` (1–86400),
`db_path=null`. Domyślnie baza operacyjna to `jobs.sqlite3` obok bazy wskazanej
przez `VDH_DB_PATH`. Checkpointy analiz znajdują się tam w `deals.sqlite3`.
Dzięki temu osobny katalog bazy izoluje testy/instancje. `db_path` pozwala
wskazać inne trwałe położenie ledgera i blokady.

`VDH_NOTIFICATIONS`: `enabled=false`, `daily_limit=50`, `timeout_seconds=10`,
`max_attempts=3` (łączna liczba prób), `backoff_seconds=2`,
`max_backoff_seconds=300`, `batch_size=50`, `sending_lease_seconds=300`.
Limit 0 blokuje nowe wysyłki produkcyjne. Kwoty alertów pochodzą z istniejących
modeli Decimal; nie przeliczamy walut ani nie generujemy nowych cen.

Rzeczywista wysyłka wymaga jednocześnie `enabled=true`, obu sekretów Telegram,
produkcyjnego źródła i zatwierdzonej analizy z pełnymi danymi alertu. Brak tokenu
nie zatrzymuje pipeline. Jeżeli znamy odbiorcę, outbox zachowuje oczekującą
wiadomość bez zajmowania limitu. Bez `CHAT_ID` nie tworzymy alertu dla zgadywanego
odbiorcy. Po uzupełnieniu danych następny cykl może ponownie zakwalifikować ofertę.
Token jest wykluczony z serializacji Settings; logger HTTPX maskuje token
zawarty w URL Telegram. Nie wykonuj rzeczywistych prób wysyłki w zwykłych testach.

## Źródło i kontekst analizy

`create_app(settings, source=adapter, context=provider)` jest punktem kompozycji.
`adapter` realizuje istniejący `OfferSource` (dla źródeł produkcyjnych preferuj
rozszerzenie `AuthorizedOfferSource`). Testowy scraper publicznych HTML Vinted PL
ma CLI oraz opcjonalną kompozycję przez `VDH_VINTED`, opisane w
[vinted_scraper.md](vinted_scraper.md). Bez `enabled=true` scheduler nadal używa
MockOfferSource. Adapter musi przestrzegać swoich limitów
i timeoutów; odczyt i obliczenia odbywają się poza pętlą zdarzeń.

`AnalysisContextProvider.prepare(offer, as_of) -> AnalysisRequest` uzupełnia
model, kategorię, opis, jawne koszty, dowody wyceny i `snapshot.listing_url`.
Nie może zmieniać danych/tożsamości oferty ani czasu odniesienia cyklu.
Domyślny `EmptyContextProvider` pozostawia nieznane koszty i brak porównań.
Nie zapisujemy automatycznie ceny ofertowej jako ceny transakcyjnej.

Link musi pochodzić ze źródła, używać HTTPS i nie zawierać danych logowania.
Nie zgadujemy linków Vinted na podstawie identyfikatora. Brak linku w zatwierdzonej
analizie oznacza pominięcie alertu, a nie generowanie fałszywego URL. Dodanie
opcjonalnego pola do JSON checkpointu nie wymaga przebudowania istniejącej tabeli.

Produkcja może użyć reviewera z ETAPU 5 po ustawieniu
`VDH_DEAL='{"ai_review_required":true}'` oraz konfiguracji AI opisanej w
[ai_reviewer.md](ai_reviewer.md). Brak klucza AI nadal oznacza deterministic-only.
Demo w schedulerze zawsze pomija AI, niezależnie od jego konfiguracji.

## Treść i deduplikacja alertów

Telegram otrzymuje HTML: nazwę i link, cenę zakupu, cenę referencyjną z podstawą
ASKING/SOLD, konserwatywną odsprzedaż, koszty, szacunkowy zysk, scoring i jego
wersję, confidence, próbę i źródła wyceny, risk level, przyczyny kwalifikacji
oraz stan AI. Wszystkie niezaufane wartości są escapowane; tekst mieści się
w limicie 4096 jednostek UTF-16. Nie pobieramy podglądów linków.

Klucz alertu to SHA-256 z `source_name + external_id + is_demo + odbiorca`.
Różne wersje analizy, scoringu, ceny i ponowne cykle nie wysyłają drugiego
alertu do tego samego odbiorcy. To świadomie konserwatywna polityka: ponowne
alerty o spadku ceny nie są jeszcze zaimplementowane. Odbiorca jest identyfikowany
hashem dokładnej konfiguracji `CHAT_ID`; zmiana odbiorcy nie przekierowuje starych
wiadomości. Stare wiadomości nie blokują kolejki aktualnego odbiorcy.

Demo korzysta wyłącznie z lokalnego `DemoNotifier`, zapisuje receipt kanału
`demo` i oznaczenie fikcyjności. Nie zużywa limitu Telegram. Transport Telegram
odrzuca dane demo również przy bezpośrednim wywołaniu.

## Trwałość, retry i granice gwarancji

Outbox i limit dobowy używają SQLite, unikalnego klucza alertu i transakcji
`BEGIN IMMEDIATE`. Przed próbą rezerwujemy jedno miejsce w dobie UTC.
Limit jest wspólny dla wszystkich odbiorców korzystających z tego samego pliku.
Sukces i nieznany wynik zachowują rezerwację; pewne odrzucenie zwalnia ją.
Północ nie powoduje ponownej wysyłki wcześniej wysłanego/niepewnego alertu.

Retry dotyczy bezpiecznych błędów przed ustanowieniem połączenia oraz jawnego
odrzucenia Telegram `ok=false` z 429 lub 5xx. Uwzględniamy `parameters.retry_after`
i nagłówek `Retry-After`; opóźnienie wynosi co najmniej backoff wykładniczy.
Przekroczenie limitu oczekiwania kończy próby zamiast wysyłać za wcześnie.
`due_at` i liczba prób są trwałe. Następny cykl odbiera zadanie dopiero po
`due_at`, więc przy interwale 300 s retry może nastąpić później niż po 2 s.
Nie ma blokującego sleep ani ukrytych retry klienta HTTP.

Read/write timeout, zerwane połączenie po wysłaniu, niejednoznaczna odpowiedź
lub przerwanie procesu dają `uncertain`, bez automatycznego retry. Sam HTTP 500
bez poprawnego potwierdzenia odrzucenia też jest niejednoznaczny. Nie gwarantujemy
exactly-once u dostawcy. Nie usuwaj tych wpisów ani rezerwacji dla obejścia limitu;
niepewna dostawa wymaga sprawdzenia po stronie operatora. Interfejs ręcznego
rozstrzygania takich wpisów pozostaje poza tym etapem.

Stare `sending` po `sending_lease_seconds` zmienia się na `uncertain`.
Transport ma timeout, a serwis dodatkowy całkowity limit oczekiwania
`timeout_seconds + 1`. Potwierdzenie Telegram najpierw trafia do outboxa,
a następnie do checkpointu `NOTIFIED`. Awaria drugiego zapisu powoduje ponowienie
samego zapisu receipt, bez ponownej wysyłki. Awaria między dostawą a zapisem
outboxa pozostaje niepewną dostawą.

## Blokowanie cykli i eksploatacja

APScheduler ma `max_instances=1`, `coalesce=true` i interwał w UTC.
Dodatkowa blokada plikowa `flock` obejmuje cały cykl — także ręczny, AI
oraz wysyłkę — i wyklucza inny proces korzystający z tego samego pliku
`jobs.sqlite3.cycle.lock`. Proces awaryjnie zakończony zwalnia blokadę systemową;
porzucony raport `running` zmienia się w `interrupted` przy następnym uruchomieniu.
Ponowienie tego samego ID zwraca ten raport; nowe ID rozpoczyna bezpieczny cykl
z deduplikacją outboxa. Pauza jest zapisana w bazie, więc działa po restarcie.
Podczas pauzy techniczne ticki APScheduler mogą się odbywać, lecz cykl nie startuje.

MVP wymaga Linux/macOS oraz lokalnego trwałego systemu plików. Zalecany jest
jeden worker schedulera. Blokada chroni przed nakładaniem w wielu procesach na
jednym hoście, ale nie jest rozproszonym lockiem dla oddzielnych maszyn/kopii
bazy; wiele instancji APScheduler może też wykonać cykle kolejno. Nie korzystaj
z NFS ani osobnych kopii bazy do uzyskania wspólnego limitu. Zapewnij backup
`offers.sqlite3`, `deals.sqlite3` i `jobs.sqlite3`; nie resetuj ledgera przy restarcie.
Tabele tworzą się idempotentnie; etap nie wprowadza Alembic ani polityki retencji.

## Testy

```sh
uv run --frozen pytest tests/operations -q
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src backtest.py
```

Testy blokują prawdziwy transport HTTP; Telegram używa MockTransport/AsyncMock.
Weryfikują brak tokenu, HTML/URL, błędy sieci, brak powtórzeń niepewnej dostawy,
retry/backoff, limit 0 i granice doby, współbieżne rezerwacje, restart, checkpoint
NOTIFIED, brak wysyłki demo, autoryzację, pauzę/wznowienie, idempotencję joba,
powtórne oferty, anulowanie żądania, blokadę między procesami oraz rzeczywisty
tick APScheduler. Żadna wiadomość testowa nie trafia do Telegram.
