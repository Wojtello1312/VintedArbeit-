# Testowe zbieranie cen z Vinted PL

Projekt jest przeznaczony do testów i zastosowań niekomercyjnych. Użytkownik
deklaruje konsultację dopuszczalności przedsięwzięcia. Dokumentacja nie traktuje
tego jako gwarancji prawnej ani uprawnienia do obchodzenia ograniczeń serwisu.

Domyślny `VintedPlaywrightOfferSource` otwiera publiczne strony
`https://www.vinted.pl` w świeżym kontekście Chromium, renderuje katalog,
wykonuje ograniczoną liczbę przewinięć, zbiera widoczne linki i przechodzi po
ofertach. Wyrenderowane JSON-LD `Product`/`Offer` lub Open Graph trafia do tej
samej walidacji i importu cen **ASKING** do SQLite. Alternatywny
`VintedOfferSource` (`--engine http`) czyta wyłącznie odpowiedź HTML bez JS.
Brak obsługiwanej struktury daje jawny błąd w raporcie, nie wymyśloną cenę.

## Uruchomienie w Windows PowerShell

W katalogu projektu zainstaluj opcjonalną bibliotekę i Chromium:

```powershell
uv sync --locked --extra browser
uv run --extra browser playwright install chromium
```

Następnie otwórz drugi terminal i wykonaj:

```powershell
# Jeden publiczny link skopiowany z przeglądarki:
$urlOferty = Read-Host 'Wklej link do oferty Vinted PL'
uv run --frozen --extra browser vinted-scraper --url $urlOferty

# Automatyczne wyszukiwanie i przejście po ofertach:
uv run --frozen --extra browser vinted-scraper `
  --search 'Nike Air Max 90' --max-pages 10 --max-requests 15 `
  --interval 5 --scroll-rounds 3
```

Chromium działa domyślnie bez okna. `--headed` pokazuje przebieg. Jeśli pobranie
Chromium jest niedostępne, a Windows ma Edge, można użyć zainstalowanej przeglądarki:

```powershell
uv run --frozen --extra browser vinted-scraper `
  --search 'Nike Air Max 90' --browser-channel msedge --headed
```

Obsługiwane kanały to `chromium`, `chrome` i `msedge`. Opcja kanału nie używa
profilu użytkownika: Playwright nadal tworzy świeży, nietrwały kontekst bez
zalogowanej sesji. Brak dodatku `browser` albo pliku wykonywalnego przeglądarki
kończy się kodem 2 i wskazówką instalacji.
Zaawansowany fallback `--browser-executable C:\ścieżka\do\chrome.exe` wskazuje
lokalny plik wykonywalny i ma pierwszeństwo przed `--browser-channel`.

Przekazanie kilku `--url` zbiera wskazane oferty w jednym przebiegu. Możesz
przekazać URL `/catalog?...` z filtrami ustawionymi w przeglądarce. `--search`
i `--url` są wzajemnie wykluczające. Nie ma automatycznej paginacji katalogu.
`--engine playwright` jest domyślny; `--engine http` zachowuje wcześniejszy tryb
bez przeglądarki. `--scroll-rounds` określa 0–20 przewinięć katalogu.

Baza domyślna to `.data/offers.sqlite3`, zgodna z domyślną aplikacją.
Przy własnej bazie serwera wskaż ją przez `--db .\moja-baza\offers.sqlite3`
lub ustaw `VDH_DB_PATH` także w drugim terminalu (zmienne terminali są niezależne).
Po imporcie odśwież `/admin/offers`, wybierz dane rzeczywiste lub wszystkie
i wyszukaj produkt. Nie trzeba restartować serwera po samym imporcie.

## Raport i znaczenie danych

Stdout zawiera JSON z `collection` i `import`; stderr zawiera logi.
`collection.pages` podaje URL, status (`collected`, `catalog`, `error`), czas
obserwacji UTC, ofertę oraz `price_basis=ASKING`. Dla błędu podaje `reason`.
`collection.requests` obejmuje robots.txt i ponowienia. `truncated=true`
oznacza zatrzymanie przy limicie stron/rekordów. `stopped_reason` opisuje blokadę
całego przebiegu. `import` podaje `new`, `skipped`, `errors` oraz powody pominięć.

Kody wyjścia: 0 — import bez błędów (może być ucięty przez jawny limit),
1 — częściowe błędy stron/rekordów, 2 — przerwany przebieg lub błąd konfiguracji/DB.
Przy przerwaniu pobierania nie są zapisywane nawet wcześniej zebrane oferty;
`import=null`. Raport zbierania może zawierać ich dane do diagnostyki.
Pole `collection.engine` wskazuje `playwright` albo `http`. W trybie Playwright
`collection.requests` liczy robots.txt i nawigacje głównego dokumentu; zasoby
strony nie są osobnymi pozycjami raportu.

Źródło ma stałą nazwę `vinted-public-pl` i `is_demo=false`. Identyfikator
metadanych musi zgadzać się z ID w pobieranym URL, aby nie pobrać ceny produktu
polecanego na tej samej stronie. Kwoty zachowują Decimal, waluta jest wymagana.
Stan `UsedCondition` oznacza `unknown`, bo sam nie określa stopnia zużycia.
Nieznana data publikacji pozostaje pusta; czas pobrania jej nie zastępuje.

Deduplikacja działa po `source_name + external_id`. Ponowne pobranie nie dodaje
duplikatu. Zmieniona cena istniejącej oferty daje `changed_existing`: importer
nie nadpisuje jej ceny i nie prowadzi historii obserwacji. Aktualną cenę widać
w raporcie zbierania. Raport nie jest automatycznie utrwalany — można zapisać go
do prywatnego pliku. Nie mieszaj fixture demo z rzeczywistymi danymi.

Import nie uruchamia analizy, AI ani powiadomień i nie zasila automatycznie
repozytorium porównań. Do wyceny potrzebne są porównywalne obserwacje i koszty.
Zniknięcie oferty, 404 i oznaczenie niedostępności nigdy nie tworzą ceny SOLD.

## Dostęp, limity i błędy

- Dozwolone adresy: HTTPS Vinted PL, `/items/<id>...` i `/catalog`.
  Prywatne API, adresy innych hostów, dane logowania i niestandardowe porty
  są odrzucane przed żądaniem. Przekierowania zatrzymują cały przebieg.
- Identyfikacja klienta: `VintedDealHunterTest/0.1`. Brak logowania i importu
  cookies użytkownika. Silnik HTTP usuwa cookies przed kolejnym żądaniem.
  Playwright może zachować cookies strony w jednym przebiegu, po czym cały
  nietrwały kontekst jest usuwany.
- Playwright uruchamia świeży kontekst bez profilu użytkownika i service workerów.
  Blokuje zasoby hostów zewnętrznych, XHR/fetch, WebSocket, obrazy, media, fonty
  oraz ścieżki `/api/`. Nie używa stealth ani przechwytywania odpowiedzi
  prywatnych endpointów.
- Robots.txt jest pobierany na początku przebiegu. Brak możliwości jego odczytu
  zatrzymuje przebieg. Uwzględniane są Allow/Disallow, wildcard `*`, końcowe `$`,
  Crawl-delay i Request-rate. Robots.txt nie jest zgodą prawną na przetwarzanie.
- Domyślnie: 10 stron, 25 żądań łącznie z robots/retry, 5 sekund odstępu,
  timeout HTTP 15 sekund na operację, maksymalnie 10 MB rozpakowanej odpowiedzi.
  Ustawienia CLI: `--max-pages`, `--max-requests`, `--interval`, `--timeout`.
  Limit wyrenderowanej treści jest niezależny od limitu plików importu. Domyślnie
  wynosi 10 MB i można go zmniejszyć opcją `--max-response-bytes` (maks. 10 MB).
- `VintedScrapeConfig` pozwala też ustawić `retries` (domyślnie 1, maks. 3)
  i `backoff_seconds` (domyślnie 2). Błędy transportu i HTTP 5xx mają ograniczone
  ponowienia z rosnącym opóźnieniem. HTTP 401/403/429 zatrzymują przebieg bez retry.
  HTTP 5xx z Retry-After również zatrzymuje przebieg. Po takim zatrzymaniu
  nie uruchamiaj kolejnych przebiegów przed końcem ograniczenia serwisu.
- Rozpoznana strona CAPTCHA/blokady zatrzymuje przebieg; nie ma obejścia.
  Wykrywanie stron blokad jest heurystyczne. Inne HTML bez danych jest zgłaszane
  jako błąd parsowania, nigdy jako poprawnie zebrana cena.
- Nie uruchamiaj równoległych scraperów. Limitowanie działa w obrębie instancji,
  nie jest dziennym limitem globalnym. CLI zakłada blokadę
  `<baza>.vinted.lock`; równoległy przebieg dla tej samej bazy kończy się kodem 2.

Jedno wywołanie wykonuje jeden ograniczony przebieg. Cykliczne uruchamianie można
skonfigurować w Harmonogramie zadań Windows, wskazując powyższe polecenie i katalog
repozytorium. Ustaw interwał dłuższy od najgorszego czasu przebiegu; blokada bazy
zabezpiecza przed nałożeniem.

Alternatywnie scheduler aplikacji może używać Playwright. W terminalu, w którym
uruchamiasz FastAPI, ustaw konfigurację i włącz harmonogram:

```powershell
$env:VDH_VINTED = '{"enabled":true,"search":"Nike Air Max 90","browser":{"max_pages":10,"max_requests":15,"interval_seconds":5,"browser_channel":"msedge"}}'
$env:VDH_SCHEDULER = '{"enabled":true,"interval_seconds":1800}'
uv run --frozen --extra browser uvicorn vinted_deal_hunter.api:create_app `
  --factory --host 127.0.0.1 --port 8000
```

`VDH_VINTED` przyjmuje `engine` (`playwright` lub `http`), dokładnie jedno z
`search` albo `urls`, oraz zagnieżdżony obiekt `browser` zgodny z limitami CLI.
Bez `enabled=true` aplikacja używa MockOfferSource. Scheduler ma własną trwałą
blokadę cyklu i `max_instances=1`, więc nie nakłada przebiegów. Ustawienia panelu
pozwalają zmienić interwał i pauzę, a sekcja System pozwala ustawić wyszukiwanie
lub publiczny adres katalogu oraz warunki pobierania.

API `/imports` nadal przyjmuje treść pliku, nie zlecenia pobierania adresów.
Scheduler domyślnie używa MockOfferSource. `VDH_VINTED` lub zapisana konfiguracja
pobierania w panelu jawnie wybierają kolektor.
Historia zmian cen pozostaje osobnym rozszerzeniem.

## Weryfikacja

```powershell
uv run --frozen pytest tests/test_vinted.py tests/test_vinted_playwright.py -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src backtest.py
```

Testy używają wyłącznie syntetycznych HTML, `httpx.MockTransport` i fałszywej
sesji przeglądarki. Sprawdzają kwoty, renderowane linki, deduplikację, robots.txt,
limity, timeout/retry, blokady, przekierowania, błędy metadanych oraz CLI.
Przejście testów nie potwierdza dostępności Vinted z konkretnego łącza ani
zgodności parsera z każdą wersją strony.

Próba rzeczywistego odczytu z tego środowiska 2026-10-19 zakończyła się błędem
DNS (`Temporary failure in name resolution`) dla `www.vinted.pl`, również po
ponowieniu poza sandboxem. Nie udało się pobrać robots.txt ani żadnej oferty.
Zgodność z bieżącym HTML Vinted i dostęp z łącza użytkownika wymagają weryfikacji.
Instalacja biblioteki Playwright w chmurze powiodła się, ale pobranie Chromium
z `cdn.playwright.dev` zwróciło `403 Domain forbidden`, również po ponowieniu
poza sandboxem. Wrapper uruchomiono z systemowym `/usr/bin/chromium`: nawigacja
do lokalnego HTML zwróciła 200, poprawny tytuł i link oferty. Pełnego testu
przeglądarki z prawdziwym Vinted nie wykonano z powodu opisanego błędu DNS.

## Konfiguracja cyklu w panelu

Źródło można teraz skonfigurować bez ustawiania JSON w terminalu:
**System → Konfiguracja pobierania**. Opis warunków, postępu, usuwania i migracji:
[admin.md](admin.md#cykl-pobierania-i-zarządzanie-ofertami).
`VDH_VINTED.filters` przyjmuje `currency`, `min_price`, `max_price`, `condition`,
`category`, `brand`, `size`. Brak `filters` zachowuje dotychczasowe pobieranie bez
filtrowania. Zapis panelu ma pierwszeństwo przed konfiguracją środowiska.
Warunki dotyczą cykli aplikacji; osobny CLI nadal używa własnych argumentów.
Pominięta oferta ma w raporcie strony `status=excluded` i kod przyczyny.
Link do ogłoszenia w panelu jest dostępny również dla wcześniej pobranych ofert.
