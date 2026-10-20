# API MVP — ETAP 8

Schemat OpenAPI: `GET /openapi.json`, interaktywna dokumentacja `/docs`.
Wszystkie kwoty pieniężne są ciągami dziesiętnymi, czasy mają UTC.
Domyślny adres nasłuchu lokalnego to port 8000 na interfejsie loopback.

## Uwierzytelnianie

`POST /imports`, `GET /offers`, `GET /offers/{key}` i wszystkie `/jobs/*`
wymagają `Authorization: Bearer <JOBS_API_TOKEN>` (minimum 32 znaki).
Brak skonfigurowanego tokenu daje 503; brak/błędny token żądania daje 401.
**Zmiana względem etapu 2:** anonimowy import HTTP jest teraz zablokowany.
CLI nie używa tokenu HTTP. Cookies panelu nie autoryzują REST API.

Panel `/admin/*` korzysta z odrębnego logowania i sesji, a każda modyfikacja
formularzem wymaga CSRF. Szczegóły: [admin.md](admin.md).

## Endpointy

| Metoda / ścieżka | Działanie |
| --- | --- |
| `GET /health/live` | 200 `{"status":"alive"}`: proces obsługuje żądania. Bez autoryzacji. |
| `GET /health/ready` | 200 `{"status":"ready"}` lub 503 `{"status":"unavailable"}`. Sprawdza cztery bazy, wersje migracji, tabele i możliwość transakcji zapisu. Bez sekretów i ścieżek; nie wywołuje AI/Telegram. |
| `POST /imports` | Import całego pliku JSON/CSV lub ręcznie zapisanego HTML; zapis ofert bez uruchamiania analizy. |
| `GET /offers` | Lista ofert z najnowszym stanem, scoringiem, zyskiem, ryzykiem i oceną ręczną. |
| `GET /offers/{key}` | Oferta oraz pełna najnowsza analiza z wyceną, powodami i historią stanów. Klucz pochodzi z listy. 404 przy braku. |
| `GET /jobs/status` | Scheduler, źródło, poprzedni cykl, liczniki i stan konfiguracji powiadomień. |
| `POST /jobs/run` | Uruchomienie cyklu; opcjonalne body `{"run_id":"manual-001"}`. Powtórzenie zakończonego ID zwraca zapisany wynik. 409 przy trwającym cyklu. |
| `POST /jobs/pause` | Trwała pauza automatycznego harmonogramu. Nie przerywa bieżącego cyklu. |
| `POST /jobs/resume` | Usunięcie pauzy. Nie włącza timera wyłączonego w ustawieniach. |

### Import

Query: `format=json|csv|html`, `source_name=demo`, `is_demo=true` (wartości domyślne).
Body to tablica rekordów JSON (`Content-Type: application/json`) albo tekst CSV
(`Content-Type: text/csv`). Format HTML wymaga `Content-Type: text/html` i
przyjmuje stronę zapisaną lokalnie przez użytkownika. Serwer nie pobiera URL,
nie uruchamia JavaScriptu i nie korzysta z prywatnych endpointów. Odczytuje
standardowe JSON-LD `Product`/`Offer` lub metadane Open Graph. Wszystkie formaty
są przesyłane bez multipart i bez ścieżek serwera.
Kontrakt pól i normalizacji: [README](../README.md#kontrakt-wejścia-i-normalizacja).

200 oznacza zakończony import; zawsze sprawdź `new`, `skipped`, `errors`,
`skip_reasons` i diagnostykę. Pojedyncze błędne rekordy nie blokują pozostałych.
413 — limit bajtów, 415 — Content-Type, 422 — struktura/query/limit rekordów,
503 — brak konfiguracji uwierzytelniania lub niedostępny zapis.
Klucz idempotencji: `(source_name, external_id)`.

### Lista ofert

Query: `q` (nazwa/ID), `brand`, `category`, `condition`, `currency` (domyślnie PLN), `min_price`,
`max_price`, `min_score`, `min_profit`, `risk=LOW|MEDIUM|HIGH|unknown`,
`label=good_deal|bad_deal|purchased|ignored`, `mode=all|demo|production`,
`sort=newest|price_asc|price_desc|score|profit|name`, `page` (od 1).
Puste opcjonalne filtry liczbowe są pomijane. Brak przeliczania walut.

Odpowiedź: `{"items":[...],"total":100,"page":1,"page_size":25}`.
Element: `key`, `offer`, `state`, `score`, `estimated_profit`, `risk_level`,
`feedback`. Brak oceny/kosztów daje `null`, nigdy wymyślone zero.

## Przykłady PowerShell

Token pobieraj ze środowiska; nie umieszczaj go w URL:

```powershell
$headers = @{ Authorization = "Bearer $env:JOBS_API_TOKEN" }
Invoke-RestMethod 'http://127.0.0.1:8000/health/ready'
Invoke-RestMethod 'http://127.0.0.1:8000/imports?source_name=demo-readiness' `
  -Method Post -Headers $headers -ContentType 'application/json' `
  -InFile '.\src\app\data\readiness_demo.json'
Invoke-RestMethod 'http://127.0.0.1:8000/imports?format=html&source_name=vinted-manual&is_demo=false' `
  -Method Post -Headers $headers -ContentType 'text/html' -InFile '.\oferta.html'
Invoke-RestMethod 'http://127.0.0.1:8000/offers?min_score=65&sort=score' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/jobs/run' -Method Post `
  -Headers $headers -ContentType 'application/json' -Body '{"run_id":"manual-001"}'
```

Import nie tworzy cen porównawczych. Do jawnie fikcyjnego pełnego scenariusza
użyj `python -m app.demo`, opisanego w [deployment.md](deployment.md).
Zwykły scheduler nadal stosuje MockOfferSource i nieznane koszty/wycenę,
jeżeli nie wstrzyknięto autoryzowanego źródła i kontekstu porównań.

### Interaktywny panel cyklu

Poniższe endpointy wymagają sesji administratora; POST wymaga formularza
`application/x-www-form-urlencoded` z `csrf`. Token REST nie zastępuje sesji.

| Metoda / ścieżka | Działanie |
| --- | --- |
| `POST /admin/collection` | Trwała konfiguracja pobierania; rewizja, enabled, search albo catalog_url, max_pages, browser_channel, currency, min_price, max_price, condition, category, brand, size. 303 po zapisie, 422 dla błędnych pól, 409 podczas cyklu/starej rewizji. |
| `POST /admin/jobs/run` | Uruchomienie w tle. Dla HTMX: 202 i fragment postępu; dla formularza: 303 do System. 409 gdy cykl już trwa. |
| `GET /admin/jobs/progress` | Fragment HTML ze stanem i licznikami, odpytywany co 2 s. |
| `POST /admin/offers/{key}/delete` | Usunięcie lokalnej oferty i zapis tożsamości blokujący ponowny import. 404 przy braku. |
| `POST /admin/offers/demo/delete` | Usunięcie lokalnych ofert demo, z zachowaniem pozostałych danych. |

`POST /jobs/run` REST nadal czeka na zakończenie i zwraca raport JSON.
Raport zawiera dodatkowo `phase`, `pages`, `excluded`, `collection_errors`
i `exclusion_reasons`. Liczniki trwającego cyklu są utrwalane w bazie jobów.
Panel domyślnie wybiera `mode=production`, `currency=all`; API REST zachowuje
wcześniejsze wartości domyślne. `currency=all` grupuje sortowanie cen po walucie;
filtry kwotowe wymagają konkretnej waluty.
