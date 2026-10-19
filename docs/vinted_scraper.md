# Testowe zbieranie cen z Vinted PL

Projekt jest przeznaczony do testów i zastosowań niekomercyjnych. Użytkownik
deklaruje konsultację dopuszczalności przedsięwzięcia. Dokumentacja nie traktuje
tego jako gwarancji prawnej ani uprawnienia do obchodzenia ograniczeń serwisu.

`VintedOfferSource` pobiera publiczne HTML z `https://www.vinted.pl`, odczytuje
JSON-LD `Product`/`Offer` albo Open Graph i importuje ceny **ASKING** do SQLite.
Obsługuje linki ofert oraz pierwszą stronę katalogu, z której zbiera linki ofert.
Nie wykonuje JavaScriptu; metadane i linki muszą znajdować się w odpowiedzi HTML.
Brak obsługiwanej struktury daje jawny błąd w raporcie, nie wymyśloną cenę.

## Uruchomienie w Windows PowerShell

W katalogu projektu, po `uv sync --locked`, otwórz drugi terminal i wykonaj:

```powershell
# Jeden publiczny link skopiowany z przeglądarki:
$urlOferty = Read-Host 'Wklej link do oferty Vinted PL'
uv run --frozen python -m vinted_deal_hunter.vinted_scraper --url $urlOferty

# Wyszukiwanie: katalog plus maksymalnie cztery strony ofert, odstęp 5 sekund:
uv run --frozen python -m vinted_deal_hunter.vinted_scraper `
  --search 'Nike Air Max 90' --max-pages 5 --max-requests 12 --interval 5
```

Przekazanie kilku `--url` zbiera wskazane oferty w jednym przebiegu. Możesz
przekazać URL `/catalog?...` z filtrami ustawionymi w przeglądarce. `--search`
i `--url` są wzajemnie wykluczające. Nie ma automatycznej paginacji katalogu.

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
- Identyfikacja klienta: `VintedDealHunterTest/0.1`. Brak logowania i przenoszenia
  cookies przeglądarki; cookies z odpowiedzi są usuwane przed kolejnym żądaniem.
- Robots.txt jest pobierany na początku przebiegu. Brak możliwości jego odczytu
  zatrzymuje przebieg. Uwzględniane są Allow/Disallow, wildcard `*`, końcowe `$`,
  Crawl-delay i Request-rate. Robots.txt nie jest zgodą prawną na przetwarzanie.
- Domyślnie: 10 stron, 25 żądań łącznie z robots/retry, 5 sekund odstępu,
  timeout HTTP 15 sekund na operację, maksymalnie 2 MB rozpakowanej odpowiedzi.
  Ustawienia CLI: `--max-pages`, `--max-requests`, `--interval`, `--timeout`.
  Limit treści pochodzi z `VDH_MAX_IMPORT_BYTES` (maks. 10 MB dla scrapera).
- `VintedScrapeConfig` pozwala też ustawić `retries` (domyślnie 1, maks. 3)
  i `backoff_seconds` (domyślnie 2). Błędy transportu i HTTP 5xx mają ograniczone
  ponowienia z rosnącym opóźnieniem. HTTP 401/403/429 zatrzymują przebieg bez retry.
  HTTP 5xx z Retry-After również zatrzymuje przebieg. Po takim zatrzymaniu
  nie uruchamiaj kolejnych przebiegów przed końcem ograniczenia serwisu.
- Rozpoznana strona CAPTCHA/blokady zatrzymuje przebieg; nie ma obejścia.
  Wykrywanie stron blokad jest heurystyczne. Inne HTML bez danych jest zgłaszane
  jako błąd parsowania, nigdy jako poprawnie zebrana cena.
- Nie uruchamiaj równoległych scraperów. Limitowanie działa w obrębie instancji,
  nie jest trwałym limitem wielu procesów ani dziennym limitem globalnym.

API `/imports` nadal przyjmuje treść pliku, nie zlecenia pobierania adresów.
Scheduler domyślnie używa MockOfferSource. Ten etap dodaje samodzielny kolektor;
automatyczna ciągła obserwacja i historia cen pozostają osobnym rozszerzeniem.

## Weryfikacja

```powershell
uv run --frozen pytest tests/test_vinted.py -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src backtest.py
```

Testy używają wyłącznie syntetycznych HTML i `httpx.MockTransport`. Sprawdzają
kwoty, deduplikację, robots.txt, limity, timeout/retry, blokady, przekierowania,
błędy metadanych oraz CLI. Przejście testów nie potwierdza dostępności Vinted
z konkretnego łącza ani zgodności parsera z każdą wersją strony.

Próba rzeczywistego odczytu z tego środowiska 2026-10-19 zakończyła się błędem
DNS (`Temporary failure in name resolution`) dla `www.vinted.pl`, również po
ponowieniu poza sandboxem. Nie udało się pobrać robots.txt ani żadnej oferty.
Zgodność z bieżącym HTML Vinted i dostęp z łącza użytkownika wymagają weryfikacji.
