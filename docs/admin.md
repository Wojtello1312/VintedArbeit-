# Panel administracyjny — ETAP 7

Panel `/admin` korzysta z FastAPI, Jinja2, HTMX 2.0.8 i lokalnie zbudowanego
Tailwind CSS 3.4.19. Działa offline. Odczytuje istniejące dane, bez automatycznego
wypełniania bazy fikcyjnymi wynikami. Pusta baza daje puste widoki.

## Uruchomienie i konto administratora

W katalogu repozytorium:

```sh
uv sync --locked
uv run --frozen python -m app.admin.password
cp .env.example .env
```

Generator prosi o hasło dwa razy (minimum 12 znaków) i wypisuje solony hash
PBKDF2-SHA256 z 600 000 iteracji. Wstaw go do `.env` jako
`ADMIN_PASSWORD_HASH='wygenerowany_hash'`. Pojedyncze cudzysłowy są konieczne,
aby powłoka zachowała znaki `$`. Nie zapisuj hasła jawnie. `.env` jest ignorowany
przez Git. Wczytaj konfigurację i uruchom jeden proces aplikacji:

```sh
set -a
. ./.env
set +a
uv run --frozen uvicorn vinted_deal_hunter.api:create_app --factory --host 127.0.0.1 --port 8000
```

Przejdź do ścieżki `/admin`. Domyślny login to `admin`, hasło wybierasz samodzielnie.
`VDH_ADMIN` (JSON) pozwala ustawić `username`, `secure_cookies`, `session_seconds`,
`login_attempts` i `login_window_seconds`. Domyślnie sesja trwa 8 godzin,
a limit logowania to 5 prób w 300 sekund na adres klienta. Nie ma domyślnego
hasła: bez hasha panel zwraca 503. AI i Telegram nie są potrzebne.

Pod HTTPS ustaw `secure_cookies=true`. Za reverse proxy ufaj nagłówkom proxy
wyłącznie od znanego serwera; poprawny schemat i host są potrzebne do kontroli
Origin, a adres klienta do ograniczenia prób logowania. Lokalny HTTP wymaga
`secure_cookies=false`. Zmiana loginu lub hasha i restart unieważniają stare sesje.

Jeżeli chcesz dane demonstracyjne, użyj istniejącego polecenia
`uv run --frozen deal-hunter` albo przycisku **System → Uruchom cykl**.
Domyślny MockOfferSource ma 130 rekordów (110 poprawnych unikalnych), z jawnym
DEMO. Brak cen porównawczych i kosztów oznacza brak wyceny/okazji; panel ich
nie wymyśla. Historyczny backtest jest osobnym narzędziem i nie zasila panelu.

## Widoki i znaczenie danych

- **Przegląd:** rzeczywiste nowe oferty (bez analizy lub stan NEW), odrzucone, okazje
  (APPROVED/NOTIFIED), średni scoring ostatnich ocen. Każda oferta liczona raz.
  Liczba AI to ukończone wyniki w ledgerze, bez powtórzeń cache. Koszt obejmuje
  znane rozliczenia według skonfigurowanych stawek; rezerwacje nieznanych kosztów
  są pokazane osobno. Powiadomienia Telegram i lokalne symulacje demo są rozdzielone.
- **Oferty:** wyszukiwanie nazwy/ID, marka, zakres ceny, minimalny scoring,
  ryzyko, zysk, etykieta, demo/produkcja i waluta; sortowanie i paginacja po 25.
  Kwoty są porównywane dokładnie przez Decimal. Domyślnie wszystkie waluty,
  filtry kwotowe wymagają jednej waluty; brak niejawnych przeliczeń. HTMX odświeża tabelę; formularze
  i linki działają również bez JavaScript.
- **Szczegóły:** ostatnia analiza, opis, bezpieczny link do ogłoszenia (z numerycznego
  ID Vinted lub zapisanej analizy), powody decyzji, ryzyko, zysk, historia etapów i wersja scoringu.
  Historia analiz ma paginację po 20. Nieznane koszty/zysk pozostają nieznane.
- **Okazje:** zatwierdzone oferty, domyślnie od najwyższego scoringu, z filtrem
  minimalnego zysku. Na stronie szczegółów można nadać `good_deal`, `bad_deal`,
  `purchased`, `ignored`. Ocena jest trwała, ma historię (ostatnie 50 wpisów)
  i nie zmienia scoringu, decyzji silnika ani nie uruchamia zakupu/powiadomienia.
- **Badanie cen:** zapisane porównania z ostatniej analizy wybranego produktu/oferty,
  do 200 najnowszych obserwacji dostępnych w jej czasie odniesienia, oraz historia
  wycen tej oferty. Widoczne źródła, liczność próby, metoda i confidence.
  ASKING, potwierdzone SOLD oraz niepotwierdzone transakcje mają odrębne etykiety.
  Nie ma pozyskiwania cen z sieci. Dane przyszłe względem analizy są pomijane.
- **System:** ostatnie 20 przebiegów, przetworzone oferty, czas, błędy,
  skonfigurowane źródło, stan schedulera i konfiguracji Telegram. Status Telegram
  nie jest testem połączenia. Sterowanie run/pause/resume korzysta z sesji i CSRF;
  istniejące JSON `/jobs/*` nadal wymagają osobnego Bearer JOBS_API_TOKEN.

Znaczniki czasu są w UTC. Dane wejściowe, wyjaśnienia AI i nieznane kody
adapterów są wyświetlane jako escapowany tekst, bez tłumaczenia treści źródłowej.
Widoki nie pokazują kluczy, tokenów, hasha hasła, chat ID ani pełnej konfiguracji.

## Ustawienia i trwałość

Formularz zapisuje tylko dozwolone pola: marki, min./maks. cenę, próg scoringu,
minimalny zysk, włączenie i interwał schedulera, włączenie AI i dzienny budżet USD.
Włączenie AI ustawia też `ai_review_required`; bez klucza/stawek/budżetu nadal
obowiązuje bezpieczny fallback reviewera. Sekrety pozostają w środowisku.

Wartości są walidowane przez Pydantic. Ustawienia zapisane w panelu mają
pierwszeństwo nad odpowiadającymi im polami środowiska po restarcie. Są stosowane
między cyklami: zapis podczas pracy cyklu zwraca 409, podobnie jak zapis formularza
ze starą rewizją. Aktywne analizy i wcześniejsze wyniki pozostają niezmienione.
Ręczna pauza jest niezależna od włączenia harmonogramu; ponowne włączenie timera
nie usuwa pauzy.

MVP uruchamiaj z jednym workerem APScheduler. Blokada cyklu chroni SQLite przed
nakładaniem analiz także między procesami, ale natychmiastowa aktualizacja timera
obejmuje proces obsługujący formularz. Inny proces wczyta politykę przed następnym
cyklem; wyłączony timer wymaga restartu tego procesu.

`app/admin` zawiera read models, sesje/CSRF, whitelistę ustawień i router.
Core pozostaje niezależny od UI. W ETAPIE 8 tabele są wersjonowane przez Alembic. Tabele `admin_sessions`, `admin_login_attempts`,
`admin_policy`, `admin_feedback`, `admin_feedback_history` są dodawane idempotentnie
do istniejącej bazy jobów. Wcześniejsze tabele nie są przebudowywane.
Odczyty panelu łączą bazy ofert, analiz i jobów w trybie read-only.
Sesje mają w bazie tylko hash losowego identyfikatora; cookies: HttpOnly, SameSite=Strict.
Token CSRF jest związany z sesją; logowanie również ma ochronę CSRF. Formularze
mają limit rozmiaru, blokadę powtórzonych pól i kontrolę Origin/Sec-Fetch-Site.
HTML ma CSP, zakaz osadzania w ramkach i cache no-store.

## Zasoby i kontrole

CSS, HTMX i licencja HTMX są dołączone do pakietu. Start nie potrzebuje Node ani
CDN. Przy zmianie klas w szablonach przebuduj CSS (Node 20+ i npm):

```sh
npm ci --ignore-scripts
npm run build
uv run --frozen pytest tests/admin
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src backtest.py
```

Testy panelu blokują rzeczywiste transporty HTTP; pokrywają sesje, uprawnienia,
CSRF, XSS, filtry, Decimal, historię, oznaczenia, koszty AI, zmianę ustawień,
restart, konflikty i wielokrotne cykle demo. Kontrola przeglądarkowa obejmuje
HTMX oraz strony na szerokościach 390 i 1440 px, bez zewnętrznych zasobów.

Od ETAPU 8 `/imports`, `/offers` i `/jobs/*` wymagają Bearer `JOBS_API_TOKEN`.
Docker Compose, migracje i instrukcje Windows: [deployment.md](deployment.md).
Pełna dokumentacja REST: [api.md](api.md).

## Cykl pobierania i zarządzanie ofertami

Po aktualizacji zatrzymaj serwer i uruchom w jego terminalu PowerShell:

```powershell
git pull origin main
$env:Path = "$HOME\.local\bin;$env:Path"
uv sync --locked --extra browser
uv run --frozen --extra browser python -m app.migrate
uv run --frozen --extra browser uvicorn vinted_deal_hunter.api:create_app --factory --host 127.0.0.1 --port 8000
```

Zachowaj `VDH_DB_PATH` i `ADMIN_PASSWORD_HASH` używane przez istniejącą instalację.
Nowy terminal wymaga ponownego ustawienia tych zmiennych; instrukcja generowania
hasła jest w [deployment.md](deployment.md). Microsoft Edge nie wymaga pobierania
Chromium. Dla kanału Chromium wykonaj `uv run --extra browser playwright install chromium`.

Jeśli cykl kończy się `SourceError`, sprawdź kod przyczyny w panelu postępu.
`chromium_not_installed` oznacza brak plików Chromium wymaganych przez daną wersję
Playwright: wybierz zainstalowany Microsoft Edge, zapisz warunki i uruchom cykl
ponownie albo wykonaj `uv run --frozen --extra browser playwright install chromium`.
Samo `uv sync --locked --extra browser` instaluje bibliotekę, ale nie przeglądarkę.
`playwright_not_installed` wymaga instalacji tego dodatku i ponownego startu serwera
z `--extra browser`. `browser_not_installed` dotyczy wybranej przeglądarki lub
niestandardowej ścieżki, a `browser_start_failed` oznacza inną awarię uruchomienia.
Nie zmieniamy automatycznie wybranej przeglądarki. Raport i log zapisują bezpieczny
kod przyczyny; surowe komunikaty procesu i ścieżki lokalne nie trafiają do panelu.
Starsze raporty nie zawierają kodu; uruchom nowy cykl po aktualizacji aplikacji.

1. W **System → Konfiguracja pobierania** zaznacz rzeczywiste źródło Vinted.
2. Podaj frazę (np. Nike Air Max 90) albo publiczny link katalogu z filtrami
   ustawionymi na Vinted. Podaj dokładnie jedno z tych pól.
3. Ustaw np. walutę PLN, cenę do 100, stan „Bardzo dobry”. Opcjonalna kategoria
   to nazwa odczytana w ogłoszeniu (np. Buty), również element ścieżki kategorii.
   Marka i rozmiar wymagają zgodności z odczytaną wartością. Puste pola nie filtrują.
4. Wybierz zainstalowaną przeglądarkę i limit stron, zapisz warunki, uruchom cykl.
5. W **Oferty** obejrzyj wyniki. Importy CLI do tej samej bazy też są widoczne.

Cena i stan trafiają do parametrów publicznego katalogu i są dodatkowo sprawdzane
na każdej pobranej ofercie. Nazwa kategorii, marka i rozmiar są sprawdzane lokalnie;
aby zawęzić katalog po taksonomii serwisu, użyj jego linku z filtrami. Nieznany stan,
rozmiar lub kategoria nie przechodzą ustawionego filtra. Brak obsługiwanych metadanych
może więc zmniejszyć liczbę importowanych ofert; licznik pominięć podaje przyczyny.
Obsługiwane są standardowe metadane oraz jawne atrybuty `item-attributes-*`
i oznaczone breadcrumbs w wyrenderowanym HTML. Zmiana struktury serwisu może
wymagać aktualizacji parsera; warunków nie wywnioskujemy z samej frazy wyszukiwania.

Cykl działa w tle i ma trwałą blokadę przed nakładaniem. Panel odpytuje stan co
2 sekundy, pokazując etap, odwiedzone strony, pasujące i nowe oferty, duplikaty,
pominięcia przez filtry, analizy i błędy. Animacja nie udaje procentu ukończenia,
bo liczba dostępnych ofert nie jest znana z góry. Przy preferencji ograniczenia
ruchu animacje są wyłączone. POST nie wymaga czekania na cały import, a zamknięcie
strony nie przerywa cyklu. Ręczne odświeżenie działa również bez JavaScriptu.
Po zakończeniu można otworzyć listę ofert. Dane w otwartej tabeli wymagają odświeżenia.

Konfiguracja źródła zapisuje się razem z wersjonowaną polityką panelu i ma
pierwszeństwo przed `VDH_VINTED`. Zmiana podczas cyklu lub zapis starego formularza
dają 409. Zwykłe ustawienia scoringu nie kasują konfiguracji pobierania. Wyłączenie
źródła Vinted przywraca źródło demo offline. Częstotliwość i automatyczne cykle
ustawisz nadal w **Ustawieniach**, a pauzę w **Systemie**.

Lista i przegląd domyślnie ukrywają demo. Listy pokazują wszystkie waluty;
nie przeliczamy SEK/DKK na PLN. Przed filtrowaniem kwot wybierz jedną walutę.
Sortowanie cen przy wszystkich walutach grupuje wyniki po walucie. „Ostatnio dodane”
korzysta z czasu importu UTC; starych czasów importu nie odtwarzamy fikcyjnie.
Starsze wpisy bez czasu są za nowymi. Pozostałe sortowania obejmują cenę, nazwę,
scoring i zysk. Scoring/zysk mogą pozostać nieznane, dopóki brakuje porównań i kosztów.

Link ogłoszenia pochodzi bezpośrednio z numerycznego ID źródła Vinted:
`https://www.vinted.pl/items/123`. Jest dostępny w tabeli i szczegółach nawet bez
analizy. ID z innymi znakami i dane demo nie służą do tworzenia takiego linku.

**Usuń** usuwa ofertę z lokalnego katalogu, **Usuń dane demo** usuwa wszystkie
lokalne oferty demonstracyjne. Formularze wymagają sesji i CSRF, a przeglądarka
pyta o potwierdzenie. Usuwanie podczas cyklu jest blokowane. Lista usuniętych
identyfikatorów zapobiega ich ponownemu importowi (`skip_reasons.deleted`) i analizie.
Historia już wykonanych analiz, koszty AI, oceny i rejestr powiadomień pozostają
w bazach audytowych. Oczekujące powiadomienia usuwanych ofert są anulowane;
wysłane lub niepewne dostarczenia zachowują swoją historię. Operacja nie usuwa ogłoszenia z Vinted.

Migracja `0003_offer_management` dodaje czas importu i tabelę usuniętych tożsamości.
Nie usuwa istniejących ofert automatycznie i nie zmienia ich dat publikacji.
