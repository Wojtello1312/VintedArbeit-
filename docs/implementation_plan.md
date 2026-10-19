# Plan realizacji Vinted Deal Hunter

Pierwotny plan architektoniczny; numeracja poniżej poprzedza szczegółowe zadania użytkownika.
Zrealizowany zakres obejmuje import (ETAP 2), matcher/wycenę (ETAP 3), Deal Engine
(ETAP 4), opcjonalny AI Reviewer (ETAP 5), alerting i scheduler (ETAP 6)
oraz panel administracyjny (ETAP 7): [admin.md](admin.md).
Outbox, Telegram, cykle i kontrola dostępu: [alerting_scheduler.md](alerting_scheduler.md).
Opis integracji AI,
limitów i testów: [ai_reviewer.md](ai_reviewer.md). Zakres został rozszerzony w ETAPIE 8 o Docker Compose, migracje Alembic, healthcheck,
chronione API ofert i test E2E: [readiness.md](readiness.md). Integracja rzeczywistego
źródła danych została rozszerzona o eksperymentalny scraper publicznych HTML
Vinted PL uruchamiany przez osobne CLI. Testy transportu korzystają z mocków;
dostępność rzeczywistych stron nie jest gwarantowana. Patrz
[vinted_scraper.md](vinted_scraper.md).
Repozytorium na początku prac nie zawierało aplikacji.
Każdy etap wymaga osobnego polecenia; zakończenie etapu nie upoważnia do
automatycznego rozpoczęcia następnego. Wszystkie etapy respektują `AGENTS.md`.

## 0. Dokumentacja — bieżący zakres

Rezultat: `AGENTS.md`, architektura modułów i niniejszy plan.
Weryfikacja: spójność wymagań, odnośniki i diff. Bez implementacji aplikacji,
instalowania stosu i tworzenia pozornych testów dokumentacji.

## 1. Szkielet i konfiguracja offline

Utworzyć pakiet Python 3.12+, konfigurację zależności i odtwarzalny lockfile,
pytest, Ruff, mypy oraz walidowaną konfigurację Pydantic. Domyślny profil demo
wyłącza sieć, AI i rzeczywistą wysyłkę. Zdefiniować konfigurowalne koszty, progi,
timeouty i limity z walidacją zakresów. Dodawać zależności w miarę potrzeb etapów.

Kryteria: import pakietu, testy konfiguracji i odrzucania błędnych wartości,
działanie bez sekretów; testy, lint i typowanie przechodzą. README opisuje
faktyczne polecenia instalacji i kontroli. Nie dodawać jeszcze całego pipeline.

## 2. Typy domenowe i demonstracyjne źródło

Wprowadzić typy pieniężne, UTC, ofertę, obserwację i dowody porównawcze,
wspólny protokół źródeł oraz adapter lokalnych danych demo. Fixture mają
jednoznaczne pochodzenie i oznaczenie demonstracyjne. Określić normalizację
tożsamości i fingerprint istotnych danych.

Kryteria: testy normalizacji, niepoprawnych kwot, walut i dat, stabilnych ID,
pustych partii i błędów źródła. Adapter działa bez dostępu do sieci i nie
interpretuje zniknięcia oferty jako sprzedaży.

## 3. Persystencja i migracje

Dodać SQLAlchemy 2.0, SQLite i Alembic, repozytoria, historię obserwacji oraz
unikalność ofert. Ustalić dokładny zapis kwot i odczyt UTC. Utworzyć izolowane
bazy testowe i transakcje. Modele analizy/outbox dodawać wraz z ich etapami.

Kryteria: migracja pustej bazy, ponowne zastosowanie aktualizacji schematu,
round-trip kwot i czasu, rollback i wielokrotny import bez duplikatów.
README zawiera sprawdzone polecenia migracji.

## 4. Deterministyczna wycena, zysk i ryzyko

Zaimplementować konfigurowalne filtry, dobór porównań, estymację, wiarygodność,
koszty, scoring i powody odrzucenia. Użyć RapidFuzz oraz pandas/NumPy tam,
gdzie pomagają w analizie, zachowując dokładność pieniężną. Zapisywać wersję
polityki i dowody wyceny. Nie generować ceny przy niewystarczających danych.

Kryteria: testy granic progów, prób zbyt małych, odstających cen, starych danych,
brakujących kosztów, zaokrągleń, strat, niezgodnych walut i powtarzalności.
Osobno sprawdzić wyceny z cen ofertowych i potwierdzonych transakcji, liczność
próby po filtracji, wyłączenie własnej oferty i pełne rozbicie zysku.

## 5. Koordynacja i powtarzalny przebieg offline

Połączyć źródło demo, repozytoria i Core przypadkiem użycia w `application`.
Dodać zapisy przebiegów, analizy, logowanie i obsługę częściowych awarii.
Wprowadzić idempotencję analiz na podstawie wersji danych i polityki.

Kryteria: test integracyjny od fixture do zapisanej analizy i lokalnego wyniku,
dwukrotne przetworzenie bez duplikatów, poprawne ponowne przeliczenie zmiany
oferty, kontrolowana awaria źródła i brak jakichkolwiek wywołań sieciowych.
README zawiera rzeczywiście zweryfikowane polecenie przebiegu demo.

## 6. REST i dashboard

Dodać FastAPI, schematy Pydantic, listę i szczegóły ofert/analiz, historię
przebiegów oraz kontrole zdrowia. Jinja2 + HTMX + Tailwind korzystają z lokalnych
zasobów. Widoki pokazują założenia, próbę, wiarygodność, ryzyko i status demo.

Kryteria: testy API i renderowania, paginacja, brak danych, błędy walidacji,
escapowanie niezaufanej treści i zgodność reprezentacji pieniędzy/czasu.
Uruchomić serwer i wykonać reprezentatywne żądanie do danych demo.

## 7. Outbox i Telegram

Dodać trwały outbox, lokalny odbiornik demo, opcjonalny klient Telegram,
deduplikację alertów, atomowe zajmowanie zadań i limity wysyłki. Sekrety
konfiguruje użytkownik poza repozytorium. Wiadomości zawierają źródło wyceny,
próbę, wiarygodność, koszty, ryzyko i link do oferty.

Kryteria: testy z atrapą transportu dla sukcesu, limitów, `429`, błędów,
timeoutów, restartu i równoległych prób. Wynik nieznany nie jest automatycznie
ponawiany; ponowna analiza nie generuje identycznego alertu. Demo nigdy nie
wysyła na Telegram. Rzeczywisty test jest opcjonalny i wymaga jawnego polecenia
wysłania wiadomości oraz skonfigurowanego kanału; nie blokuje trybu offline.

## 8. Opcjonalna weryfikacja AI

Dodać adapter OpenAI, model odpowiedzi Pydantic, shortlistę, wersjonowane
instrukcje, ochronę granicy niezaufanej treści i trwały budżet wywołań.
Reviewer nie wycenia ani nie uruchamia narzędzi. Udokumentować obsługę veto,
braku klucza, błędów i wyczerpania budżetu.

Kryteria: testy bez sieci dla poprawnej i błędnej odpowiedzi, prompt injection,
timeoutu, limitów i restartu. Pełny przebieg działa bez klucza, a pominięte AI
nie jest przedstawiane jako zatwierdzona weryfikacja. Raportować udział AI;
nie zwiększać wywołań tylko dla osiągnięcia procentowego celu.

## 9. Scheduler i eksploatacja kontenerowa

Dodać APScheduler w jednym procesie, ochronę przed nakładaniem przebiegów,
kontrolowane zamykanie oraz Dockerfile i Docker Compose. Utrwalać SQLite,
oddzielić migracje od uruchamiania procesów i zapewnić lokalne zasoby dashboardu.

Kryteria: start Compose, migracje, funkcjonalny przebieg demo, restart z
zachowaniem danych i limitów, brak duplikatów alertów oraz brak powielonego
schedulera. README opisuje instalację, start/stop, konfigurację, backup
i odtworzenie bazy oraz ograniczenia SQLite i dostarczania powiadomień.

## 10. Autoryzowane źródło i przegląd gotowości MVP

Na zlecenie użytkownika dodano testowy adapter publicznych HTML Vinted PL.
Użytkownik deklaruje wcześniejszą konsultację przedsięwzięcia; nie stanowi to
potwierdzenia prawnego po stronie projektu. Dokumentować sposób dostępu, limity,
retencję i pochodzenie dowodów transakcyjnych. Brak źródła nie blokuje demo.
Nie zakładać istnienia publicznego API Vinted ani stosować obejść.

Kryteria: wspólne testy kontraktowe adapterów, testy timeoutów i błędów,
oddzielenie danych rzeczywistych od demo, przegląd sekretów i logów oraz pełny
test end-to-end offline. Testy rzeczywistego źródła są osobne i zależą od dostępu.
Publiczna ekspozycja API wymaga dodatkowego zakresu dotyczącego autoryzacji.

## Kontrole na każdym etapie

Po zmianach kodu uruchamiać adekwatne testy, Ruff i mypy zgodnie z aktualnym
README. Raport podaje wykonane polecenia, wyniki, pominięte kontrole i blokery.
Etap jest zakończony dopiero po spełnieniu kryteriów i aktualizacji dokumentacji;
niepowodzeń nie maskujemy pomijaniem testów ani fikcyjnymi wynikami.
