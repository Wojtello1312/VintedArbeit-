# Uruchomienie i utrzymanie MVP

Wymagania lokalne: Python 3.12+ i uv. Docker: Docker Engine / Docker Desktop
z Compose v2 i obsługą linuksowych kontenerów. Jeden worker aplikacji/schedulera,
lokalny dysk SQLite. Nie umieszczaj baz na współdzielonym dysku NFS/SMB.

## Windows PowerShell — uruchomienie natywne

W katalogu repozytorium:

```powershell
uv sync --locked
$env:VDH_DB_PATH = Join-Path $PWD '.data\offers.sqlite3'
$env:VDH_SCHEDULER = '{"enabled":false,"interval_seconds":300}'
$env:VDH_AI = '{"enabled":false}'
$env:VDH_NOTIFICATIONS = '{"enabled":false}'
$env:ADMIN_PASSWORD_HASH = (uv run --frozen python -m app.admin.password).Trim()
$env:JOBS_API_TOKEN = (uv run --frozen python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
uv run --frozen python -m app.migrate
uv run --frozen python -m app.demo
uv run --frozen uvicorn vinted_deal_hunter.api:create_app --factory --host 127.0.0.1 --port 8000
```

Generator hasha pyta interaktywnie o hasło (minimum 12 znaków) i potwierdzenie.
Login panelu to `admin`. Otwórz w przeglądarce ścieżkę `/admin` na lokalnym porcie
8000. Zmienne sesji PowerShell nie przetrwają zamknięcia terminala; zapisz swoje
ustawienia prywatnie. `.env` nie jest automatycznie czytany przez natywne
`uvicorn`. Na Windows nie używaj polecenia shellowego `. ./.env`.

Kontrole:

```powershell
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src backtest.py
```

Blokady używają portalocker zamiast importu fcntl. CI zawiera macierz Linux /
Windows, ale lokalna weryfikacja tego etapu odbyła się na Linux; nie traktuj
samego pliku CI jako dowodu zakończonego testu na Windows.

## Docker Compose

Ustaw `ADMIN_PASSWORD_HASH` i `JOBS_API_TOKEN` jak powyżej, jeżeli masz Python/uv.
Możesz też zbudować obraz bez Pythona na hoście i wygenerować dane w kontenerze:

```powershell
docker compose build
docker compose run --rm --no-deps app python -m app.admin.password
```

Wynik generatora wstaw prywatnie do `.env` w pojedynczych cudzysłowach.
Przykład struktury jest w `.env.example`; nie zapisuj hasła jawnie.
Token API generuje `docker compose run --rm --no-deps app python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Przechowuj go prywatnie w `JOBS_API_TOKEN`. Compose odczytuje `.env` i zmienne
powłoki (powłoka ma pierwszeństwo). Nigdy nie commituj `.env`.

```powershell
docker compose up -d --build --wait
Invoke-RestMethod 'http://127.0.0.1:8000/health/ready'
docker compose exec app python -m app.demo
docker compose ps
docker compose logs --tail 100 app
# Zatrzymanie z zachowaniem danych:
docker compose down
# Ponowny start używa tego samego wolumenu:
docker compose up -d --wait
```

Domyślnie port jest dostępny wyłącznie na `127.0.0.1`, AI i Telegram są wyłączone.
`VDH_PORT` zmienia port hosta. Kontener działa jako UID/GID 10001, ma read-only
root filesystem, `/tmp` w tmpfs i jedyny trwały zapis w `/data`.
Dockerfile instaluje zamrożony lockfile i pakiet zawierający szablony, HTMX,
CSS, demo i migracje. Nie kopiuje `.env`, baz, Git ani testów do obrazu.
Komenda startowa wykonuje migracje przed `uvicorn`; ich błąd blokuje start.
Bez hasha panel daje 503, bez tokenu chronione API daje 503. Healthcheck nadal
może być zdrowy — sprawdza aplikację i bazy, a nie kompletność poświadczeń.

W środowisku z firmowym proxy TLS użyj zaufanego **pełnego pakietu CA**:

```sh
docker build --secret id=build_ca,src=/ścieżka/do/ca-bundle.crt -t vinted-deal-hunter-app .
docker compose up -d --no-build --wait
```

BuildKit udostępnia certyfikat tylko podczas budowy. Nie wyłączaj weryfikacji
TLS. Sekret `build_ca` jest opcjonalny i nie trafia do końcowego obrazu.
Po zmianie kodu przebuduj obraz. Obraz bazowy `python:3.12-slim` pozostaje
aktualizowanym tagiem; dla konkretnego wydania przypnij zatwierdzony digest
po sprawdzeniu aktualizacji bezpieczeństwa.

## Demo

`python -m app.demo` importuje dokładnie 100 unikalnych ofert ze
`src/app/data/readiness_demo.json` i uruchamia analizę z jawnym fikcyjnym kontekstem.
Domyślna polityka daje 80 zakwalifikowanych, 10 odrzuconych i 10 wymagających
weryfikacji. Cena referencyjna pochodzi z 20 fikcyjnych transakcji DEMO;
nie jest ceną rzeczywistego rynku. Mock powiadomień zapisuje lokalne potwierdzenia.
CLI nie inicjalizuje klienta OpenAI ani Telegrama, nawet jeśli klucze są dostępne.
AI jest testowane osobno w E2E przez wstrzyknięty mock. Ponowne polecenie używa
stałego czasu historycznego i tożsamości; nie tworzy nowych ofert ani alertów.
Nie uruchamiaj seedera w trakcie cyklu schedulera.

## Bazy i migracje

Cztery role SQLite: `offers.sqlite3`, `deals.sqlite3`, `jobs.sqlite3`, `ai.sqlite3`.
Domyślnie wszystkie są rodzeństwem `VDH_DB_PATH`. Jawne `VDH_SCHEDULER.db_path`
i `VDH_AI.db_path` mogą nadpisać lokalizacje — w Dockerze muszą wskazywać trwałe
wolumeny. Różne role nie mogą współdzielić jednego pliku.

Przy aktualizacji starej instalacji z niestandardowym `VDH_DB_PATH` ledger AI
mógł pozostać w `.data/ai.sqlite3`. Jeśli taki plik istnieje, a nowe domyślne
położenie byłoby inne, konfiguracja środowiskowa blokuje start. Ustaw jawnie
`VDH_AI.db_path` na stary ledger lub przenieś go wraz z pozostałymi bazami
po zatrzymaniu procesów. Nie twórz pustego ledgeru w celu obejścia blokady: utraciłbyś
historię kosztów i deduplikację.

```sh
uv run --frozen python -m app.migrate
```

Alembic: `0001_baseline` tworzy schemat lub bez utraty danych przejmuje zgodne
bazy etapów 2–7; porównuje kolumny starego schematu przed przypisaniem wersji.
`0002_indexes` dodaje indeksy historii analiz, jobów, alertów i ocen ręcznych.
Każdy plik ma `alembic_version` i `database_role`. Migracje są serializowane
blokadą pliku, działają transakcyjnie i są powtarzalne. Nieznany schemat lub
wersja blokują aktualizację. Przerwanie między bazami można ponowić: każda rola
ma własną transakcję. Przy przyszłych zmianach dodawaj nową rewizję; nie zmieniaj
opublikowanego baseline. Destrukcyjne downgrade są wyłączone.

### Kopia i odtworzenie

Zatrzymaj aplikację i wszystkie CLI korzystające z baz. Kopiuj cały katalog,
łącznie z ewentualnymi journal/WAL/SHM; nie kopiuj pojedynczych plików w trakcie
zapisu. Budżety, deduplikacja, oceny i sesje wymagają zachowania wszystkich baz.

Docker (polecenia kompatybilne z PowerShell, bez przekierowania binarnego stdout):

```powershell
docker compose stop app
docker compose cp app:/data ./backup-data
# Odtworzenie z wybranej kopii; aplikacja nadal zatrzymana:
docker compose cp ./backup-data/. app:/data
docker compose run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint sh app -c "chown -R 10001:10001 /data"
docker compose start app
Invoke-RestMethod 'http://127.0.0.1:8000/health/ready'
```

Jednorazowe `--cap-add CHOWN` jest potrzebne tylko przy przywracaniu właściciela
plików po `docker compose cp`; zwykły proces aplikacji nadal ma `cap_drop: ALL`.

Przy odtwarzaniu do innej wersji uruchom zgodny obraz lub wykonaj migracje przed
startem aplikacji. Katalog kopii powinien być nowy; chroń backup jak bazę produkcyjną.
Natywnie po zatrzymaniu procesu skopiuj cały `.data` przez `Copy-Item -Recurse`.
`docker compose down` zachowuje wolumen; `down -v` **usuwa wszystkie dane**.

## Granice wdrożenia

SQLite i blokada plikowa obsługują jeden host, jeden worker schedulera. Brak HA,
replikacji, wieloużytkownikowego RBAC i automatycznej rotacji sekretów.
Przed udostępnieniem poza loopback skonfiguruj HTTPS, Secure cookies, kontrolę
proxy i operacyjne kopie danych. Domyślny obraz wyłącza zaufanie proxy headers;
przy reverse proxy zmień komendę Uvicorn i ufaj wyłącznie znanemu adresowi proxy.

Limit AI używa atomowych rezerwacji przy jawnych stawkach USD/token; nieznana
odpowiedź zachowuje rezerwację. Jest to limit aplikacyjny, nie gwarancja rachunku
operatora API przy błędnych stawkach/zmianie cennika. Ustaw również limit projektu
u dostawcy przed włączeniem płatnych usług. Niepotwierdzone dostarczenie Telegram
pozostaje `uncertain`, bez automatycznej ponownej wysyłki.
