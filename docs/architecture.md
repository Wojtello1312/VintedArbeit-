# Architektura Vinted Deal Hunter

Rozszerzenie testowe: `vinted_deal_hunter/vinted.py` implementuje `OfferSource`
przez publiczne HTML Vinted PL, a `vinted_scraper.py` jest jawnym wejściem CLI.
Warstwa HTTP sprawdza robots.txt, ogranicza rozmiar, liczbę i tempo żądań,
zatrzymuje się przy blokadach i przekazuje metadane do istniejącego parsera HTML.
CLI wykorzystuje wspólny importer i repozytorium SQLite. Raport zawiera źródłowy
URL, czas obserwacji UTC oraz ceny ASKING; nie tworzy dowodów SOLD.
API i domyślny scheduler nie uruchamiają scrapera. Szczegóły i ograniczenia:
[vinted_scraper.md](vinted_scraper.md).

Bieżąca implementacja ETAPU 6: [alerting_scheduler.md](alerting_scheduler.md).
`app/notifications` realizuje trwały outbox i transport Telegram;
`app/scheduler` odpowiada za cykle, blokadę, raporty i chronione sterowanie.

Panel ETAPU 7 znajduje się w `src/app/admin`: router Jinja2/HTMX, read models,
sesje/CSRF, trwałe ustawienia i oceny ręczne. Szczegóły: [admin.md](admin.md).

Poniżej architektura docelowego MVP; stan wdrożonych etapów opisują powyższe
dokumenty modułów. ETAP 8 dodaje `app/storage/migrations.py`, pakiet rewizji
`app/migrations`, `app/health.py`, `app/read_api.py`, wspólną autoryzację REST
`app/security.py` i jawny scenariusz `app/demo.py`. Wdrożenie: [deployment.md](deployment.md).

## Przepływ i tryby pracy

Autoryzowane źródło → normalizacja → zapis obserwacji → filtry → dobór porównań
→ wycena → zysk i ryzyko → ranking → opcjonalny reviewer AI → outbox → Telegram.
API i dashboard udostępniają dane oraz uzasadnienia decyzji z bazy.

Domyślny profil `demo` korzysta wyłącznie z lokalnych fixture, lokalnej bazy,
wyłączonego AI i lokalnego odbiornika powiadomień. Wszystkie zasoby interfejsu
muszą być dostępne lokalnie; dashboard nie wymaga CDN. Profil zewnętrzny wymaga
jawnego włączenia konkretnych adapterów i ich konfiguracji. Dane demo są
oznaczone i oddzielone od danych rzeczywistych; nie wolno wysyłać ich na Telegram.

## Moduły i odpowiedzialności

Planowany układ: `src/vinted_deal_hunter/` z poniższymi modułami oraz `tests/`.

| Moduł | Odpowiedzialność i granice |
| --- | --- |
| `sources` | Wspólny protokół pobierania ofert, adapter demo oraz późniejsze adaptery autoryzowane. Normalizacja danych i metadane pochodzenia, paginacja i błędy źródła. |
| `core` | Typy domenowe, filtry, dopasowanie porównań, wycena, koszty, zysk, ryzyko i ranking. Czyste funkcje, bez sieci i ORM. RapidFuzz do podobieństwa; pandas/NumPy do statystyk, bez obliczeń pieniężnych na float. |
| `application` | Koordynacja przebiegu, interfejsy repozytoriów i usług, transakcje, wersje polityk oraz decyzja o utworzeniu alertu. Wspólny przypadek użycia dla API i schedulera. |
| `ai` | Opcjonalny reviewer shortlisty przez OpenAI API, walidowany wynik Pydantic, limity i ewidencja użycia. Nie wycenia i nie nadpisuje faktów. |
| `db` | Modele SQLAlchemy 2.0, implementacje repozytoriów, transakcje, SQLite i migracje Alembic. |
| `api` | FastAPI, schematy REST, obsługa błędów, Jinja2 + HTMX + lokalny Tailwind. Brak logiki wyceny w endpointach. |
| `scheduler` | APScheduler uruchamia przypadki użycia, kontroluje nakładanie przebiegów i inicjuje wysyłkę outbox. |
| `notifications` | Lokalny odbiornik demo i klient Telegram Bot API, renderowanie alertów, limity, timeouty i obsługa wyników dostarczenia. |
| `config` | Walidowana konfiguracja, profile, koszty, progi, harmonogramy, budżety i ustawienia usług. |
| `observability` | Konfiguracja logowania, identyfikatory przebiegów i pomiary etapów. |

Interfejs źródła zwraca partie ujednoliconych ofert wraz z kursorem i metadanymi
pochodzenia. Powinien jednoznacznie rozróżniać pusty wynik od awarii. Adapter nie
decyduje, czy oferta jest okazją. Interfejsy oraz konkretne nazwy metod zostaną
ustalone w odpowiednim etapie implementacji.

## Model danych i dowody wyceny

- `Listing`: źródło, stabilne ID, URL, tytuł, opis, cechy produktu, stan,
  cena ofertowa z walutą, czas źródłowy (jeśli znany), czas pozyskania UTC,
  oznaczenie demo oraz fingerprint istotnych danych.
- `ListingObservation`: historia cen i stanów widoczności; brak oferty przy
  kolejnym pobraniu nie tworzy transakcji sprzedaży.
- `Comparable`: pochodzenie, cechy, cena, waluta, czas i rodzaj dowodu:
  cena ofertowa albo potwierdzona cena transakcyjna. Potwierdzenie transakcji
  wymaga dowodu dostępnego zgodnie z uprawnieniami źródła.
- `Valuation`: metoda i wersja polityki, źródła, liczność próby po filtracji,
  okres danych, rodzaj użytych cen, estymata/przedział, wiarygodność i powody
  obniżenia wiarygodności. Możliwy wynik: niewystarczające dane.
- `Analysis`: referencja do wersji oferty i wyceny, rozbicie kosztów, potencjalny
  zysk, stopa zwrotu z jawnym mianownikiem, ryzyko, scoring i uzasadnienia.
- `AIReview`: wersja modelu/promptu, wynik walidacji, decyzja, status i zużycie.
- `ProcessingRun`, `NotificationOutbox`, `UsageBudget`: przebiegi, klucze
  deduplikacji, stany wysyłki i trwałe liczniki limitów.

Pieniądze w domenie to `Decimal` i waluta, w SQLite dokładne jednostki
najmniejsze z jawną skalą. Na granicy JSON kwoty są ciągami dziesiętnymi.
Daty są świadome strefy; zapis i odczyt zachowują semantykę UTC.

Dobór porównań bierze pod uwagę kategorię, markę/model, stan, rozmiar, walutę,
świeżość i podobieństwo tekstowe. Reguły odrzucania obserwacji odstających,
minimalna próba i progi wiarygodności podlegają konfiguracji. Nie mieszamy
milcząco cen transakcyjnych i ofertowych; wycena oparta wyłącznie o ceny ofertowe
ma jawne ograniczenie wiarygodności. Bieżąca oferta nie jest własnym porównaniem.

Potencjalny zysk = estymowana cena odsprzedaży − cena zakupu − wysyłka zakupu
− koszty odsprzedaży − prowizje − rezerwy. Założenia i zaokrąglenia są widoczne.
Brak danych o koszcie nie oznacza automatycznie kosztu zerowego; stosujemy jawny
parametr lub oznaczamy niekompletność. Różne waluty są odrzucane w MVP, dopóki
nie powstanie jawna polityka kursów. Scoring nie jest obietnicą zysku.

## Determinizm i AI

Wszystkie oferty przechodzą deterministyczne reguły. Cel około 90–95% analizy
deterministycznej realizujemy przez ograniczenie AI do końcowej shortlisty;
raportujemy liczbę ofert na każdym etapie i udział wywołań AI. Nie wymuszamy
wywołań modelu, aby osiągnąć procent: bez AI udział deterministyczny wynosi 100%.

Reviewer zwraca ustrukturyzowaną decyzję i powody w modelu Pydantic. Opis oferty
jest wydzielonym niezaufanym polem danych. Model nie ma narzędzi wykonujących
zakupy ani dostępu do sekretów. Walidacja, ograniczenie długości wejścia i wyjścia
oraz timeout są obowiązkowe. Błąd, brak klucza lub wyczerpanie limitu zapisuje
status pominięcia i zachowuje wynik deterministyczny; alert nie może twierdzić,
że został zweryfikowany przez AI. Jawne veto reviewera wstrzymuje alert.

## Idempotencja, harmonogram i dostarczenie

Unikalność `(source, external_id)` chroni oferty, a fingerprint danych i wersja
polityki identyfikują analizę. Powtórne pobranie identycznej oferty nie tworzy
nowej analizy. Zmiana polityki może przeliczyć ofertę, ale sama nie odblokowuje
ponownego alertu. W MVP jeden alert na ofertę i kanał docelowy; ewentualna polityka
ponownych alertów wymaga osobnego etapu i testów.

Zapis analizy i wpisu outbox odbywa się atomowo. Unikalny klucz alertu i atomowe
zajęcie wpisu zapobiegają równoległej wysyłce. Stany obejmują oczekiwanie,
wysyłanie, dostarczenie, bezpieczny retry, błąd i wynik nieznany. Telegram nie
gwarantuje idempotencji: timeout po wysłaniu lub restart w trakcie wysyłki oznacza
stan nieznany wymagający przeglądu, bez automatycznego ponowienia. To świadomy
kompromis ograniczający duplikaty kosztem możliwej utraty alertu.

MVP używa jednego procesu schedulera i jednego workera outbox. Uruchamianie
wielu workerów API nie może powielać schedulera. SQLite wymaga krótkich transakcji,
kontroli blokad i testów współbieżności. Harmonogram działa w UTC. Ograniczenie
nakładania zadań uzupełniają trwałe ograniczenia bazy. Limity AI i powiadomień
są rezerwowane atomowo i przeżywają restart.

## Eksploatacja, interfejs i walidacja

API udostępni oferty, szczegóły analizy, historię przebiegów i stan zdrowia.
Dashboard pokaże źródło, próbę i wiarygodność wyceny, rozbicie kosztów, ryzyko
oraz status AI i alertu. Nie będzie przycisków automatycznego zakupu/kontaktu.
Tekst ofert musi być escapowany w HTML i wiadomościach Telegram.

Pierwsze wdrożenie jest lokalne, z wiązaniem interfejsu do loopback. Publiczne
udostępnienie wymaga osobnej decyzji o uwierzytelnianiu i ochronie operacji
zmieniających stan. Sekrety wyłącznie w środowisku, logi bez tokenów i zbędnych
danych osobowych. Przechowywanie surowych danych ograniczamy do potrzeb audytu,
z polityką retencji zgodną z warunkami źródła.

Docker Compose zapewnia trwały wolumen SQLite, migracje i jawny cykl start/stop.
Gotowość nie oznacza jedynie otwartego portu: sprawdzamy dostęp do bazy i
reprezentatywny przebieg offline. Testy obejmą domenę, adapter demo, migracje,
API, awarie klientów, idempotencję oraz działanie bez OpenAI i bez sieci.
