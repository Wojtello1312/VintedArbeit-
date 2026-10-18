# ETAP 8 — raport przeglądu MVP

Przegląd wykonano 18 października 2026. Zakres: offline MVP, kod aplikacji,
importy, persystencja, autoryzacja, limity usług, E2E i uruchomienie kontenerowe.
Raport nie oznacza certyfikacji publicznego, wieloużytkownikowego wdrożenia.

## Wykryte problemy i naprawy

| Problem | Naprawa / dowód |
| --- | --- |
| `POST /imports` pozwalał anonimowo modyfikować bazę. | Wspólne Bearer auth dla importu, odczytu ofert i `/jobs/*`; testy 401/503 i poprawnego dostępu. |
| Bezwarunkowy import `fcntl` blokował start na Windows. | Portalocker dla blokady procesu i migracji; test importów oraz współbieżnych operacji na Linux. Windows pozostaje do uruchomienia w CI. |
| Pomocniki importowane z niekwalifikowanego `conftest` powodowały błędy zbierania testów przy innej kolejności katalogów. | Wydzielony `tests.admin.helpers`, pakiety testowe i importy kwalifikowane; przetestowano również niestandardową kolejność zestawów. |
| Ledger AI mógł pozostawać w innym katalogu niż niestandardowa baza ofert, poza oczekiwanym backupem/wolumenem. | Domyślny ledger jest obok bazy ofert, jawne ścieżki są zachowane; wykrycie starszego ledgeru blokuje ciche porzucenie historii kosztów. |
| Brak wersjonowania schematu, healthchecka, obrazu i pełnego E2E. | Dwie rewizje Alembic, sondy live/ready, Dockerfile, Compose i test 100 ofert od importu do dashboardu. |

Dodatkowo komunikaty walidacji wspólnych schematów ukrywają wejściowe wartości,
aby ograniczyć przypadkowe ujawnienie sekretów błędnie umieszczonych w konfiguracji.
Sekrety nie trafiają do odpowiedzi API/HTML ani model_dump ustawień. Istniejące
testy sprawdzają również maskowanie tokenu w URL Telegrama oraz logi reviewera.

## Architektura

Źródła i normalizacja pozostają wymienne. Core nie importuje FastAPI,
SQLAlchemy, OpenAI, schedulera ani implementacji storage. AI i powiadomienia są
osobnymi adapterami; pipeline przechowuje checkpointy i uzasadnienia decyzji.
Panel i nowe REST API używają read models, nie implementują ponownie wyceny.
Migrations, auth i healthcheck są oddzielone od algorytmów domenowych.

Test AST sprawdza cykle importów i granice Core. Import modułów aplikacji jest
sprawdzany w osobnym interpreterze; `migrations/env.py` jest skryptem Alembic,
a nie samodzielnym modułem do importowania. W badanym grafie nie znaleziono
cyklicznych zależności.

Cztery pliki SQLite mają własne transakcje: nie ma jednej transakcji obejmującej
import, analizę i dostarczenie. Spójność po przerwaniu zapewniają klucze unikalne,
CAS checkpointów i reconciliation outboxu. Nie obiecujemy exactly-once przy
niejednoznacznej odpowiedzi zewnętrznego dostawcy.

## Scenariusz E2E

1. `POST /imports` przyjmuje 100 fikcyjnych rekordów. Powtórny import raportuje
   0 nowych i 100 pominiętych. Normalizacja potwierdzona w zapisanych rekordach.
2. Pipeline wykonuje filtry, matching, medianę/IQR i scoring na 20 jawnie fikcyjnych
   transakcjach DEMO. Wynik domyślny: 80 okazji, 10 odrzuconych, 10 do weryfikacji.
3. Wstrzyknięty mock reviewera daje 80 walidowanych wyników; ledger zapisuje
   tokeny i syntetyczny koszt **0,056 USD** przy testowych stawkach, bez faktury
   ani płatnego wywołania. Koszt ten dotyczy wyłącznie fixture testowej.
4. Mock odbiornika Telegram/demo zapisuje 80 lokalnych potwierdzeń i stany
   NOTIFIED. Nie tworzy połączenia z Telegramem; adapter HTTP ma osobne testy.
5. Ponowne przetworzenie i restart zachowują 100 ofert, 80 NOTIFIED, 80 wywołań
   mocka AI i 80 mockowanych dostarczeń. Brak kolejnych wywołań i kosztów.
6. FastAPI odczytuje wyniki, a uwierzytelniony dashboard pokazuje okazje DEMO.

Samodzielny `python -m app.demo` nie używa nawet mockowanego SDK AI: pomija AI
jawnie i wykonuje cały pozostały przebieg offline. Test obejmuje dwukrotne
uruchomienie tej komendy, także przy obecnym kluczu środowiskowym, bez jego użycia.

## Limity, migracje i trwałość

Testy budżetu obejmują atomową rezerwację przy równoległych workerach,
limit USD na dokładnej granicy, limit liczby analiz, restart, UTC rollover,
cache, retry i utrzymanie rezerwacji przy nieznanym usage. Przy limicie dwóch
rezerwacji 20 współbieżnych prób dopuszcza dokładnie 2, odrzuca 18.
Istniejące testy powiadomień obejmują dzienny limit i niejednoznaczną wysyłkę.

Migracje zweryfikowano na pustych bazach i schemacie z etapu 7: zachowane zostały
oferty, rewizje analiz, oceny, pauza i ledger kosztów. Powtórzenie jest idempotentne;
nieznany schemat nie jest stemplowany, a błędna migracja cofa transakcję.
Testy sprawdzają równoległy start i pomylenie ról baz. Healthcheck wykrywa brak
tabeli bez ujawnienia ścieżek lub szczegółów błędu.

## Weryfikacja i ograniczenia

Wykonane kontrole:

- Pełny pytest: **407 passed**, jedno ostrzeżenie deprecacji TestClient.
- Ruff: bez błędów; `ruff format --check .`: 115 plików zgodnych z formatem.
- mypy: brak problemów w 69 plikach źródłowych.
- Docker: obraz zbudowany, uruchomiony UID 10001, readiness healthy.
  Sprawdzono migracje i zasoby pakietu, demo 100, chroniony REST i dashboard.
  Odtworzenie kontenera oraz `down/up` zachowały 100 ofert i 80 NOTIFIED.
- Backup offline i odtworzenie czterech ról: integralność SQLite i ponowny
  healthcheck poprawne. Próba ujawniła potrzebę jednorazowego `--cap-add CHOWN`
  przy odtwarzaniu właściciela plików; instrukcja została poprawiona.
- Build w chmurze wymagał przekazania zaufanego pakietu CA proxy przez sekret
  BuildKit; TLS pozostał weryfikowany.

Polecenia odtwarzające: `pytest`, `ruff check .`, `ruff format --check .`,
`mypy src backtest.py`. Testy readiness blokują rzeczywiste transporty HTTP.

Pozostałe ograniczenia:

- Natywnego Windows PowerShell nie uruchomiono w tym środowisku Linux.
  Plik CI zawiera macierz Windows/Linux; publikacja pliku nie oznacza wykonania CI.
- Nie testowano realnego Vinted, OpenAI ani Telegrama; świadomie nie wysyłano
  płatnych zapytań ani rzeczywistych alertów. Produkcyjny adapter wymaga autoryzacji
  i rzeczywistych porównań cenowych.
- Jeden host/worker, SQLite, brak HA i wieloużytkownikowego RBAC.
  HTTPS, polityka reverse proxy, backupy i rotacja sekretów pozostają obowiązkiem
  operatora przed udostępnieniem aplikacji poza interfejs loopback.
- TestClient zgłasza ostrzeżenie deprecacji integracji Starlette z httpx.
  Testy nadal działają; aktualizacja klienta testowego wymaga osobnego sprawdzenia.
- Limit AI jest oparty na jawnie skonfigurowanych stawkach. Należy utrzymywać
  aktualny cennik i niezależny limit projektu dostawcy API.

Instrukcje uruchomienia, backupu i odtworzenia: [deployment.md](deployment.md).
Kontrakt endpointów: [api.md](api.md).
