# Product Matcher i Market Pricing — metodologia

Etap 3 dodaje bibliotekę offline w `src/app/core/product_matcher.py`,
`src/app/core/price_estimator.py` i `src/app/schemas/pricing.py`. Nie pobiera cen
z sieci. `ObservationSource.fetch_comparables()` jest kontraktem adaptera;
`estimate_from_source()` pobiera dane i przepuszcza awarię źródła do wywołującego.
`estimate()` przyjmuje bezpośrednio obserwacje. Brak danych i awaria to różne wyniki.

## Matching

Nazwa jest normalizowana przez NFKC, casefold, scalenie odstępów i zamianę
interpunkcji na odstępy. Marka, model i rozmiar używają NFKC, casefold i odstępów;
marka dodatkowo jawnych aliasów H&M. Nie zgadujemy modelu z nazwy ani równoważności
rozmiarów z różnych systemów. Puste metadane pozostają `None`.

Marka i rozmiar muszą być znane i zgodne. Różne jawne modele zawsze odrzucają
porównanie, nawet przy identycznej nazwie i marce. Gdy obydwa modele są nieznane,
wymagamy dokładnej zgodności znormalizowanej nazwy i zgłaszamy `model_unverified`;
model znany tylko po jednej stronie odrzuca porównanie. Stan musi być znany;
domyślnie dopuszczamy wyłącznie taki sam stan.

Po tych warunkach nazwy zgodne dokładnie otrzymują 100 punktów; pozostałe
porównuje RapidFuzz `token_sort_ratio`. Wynik `product_match_score` to średnia
ważona: nazwa 45%, marka 15%, model 20%, rozmiar 10%, stan 10%. Brak modelu
daje temu składnikowi 0. Próg wynosi 80/100. Zwracamy jawne powody odrzucenia,
np. `model_mismatch`, `size_mismatch`, `condition_unknown`, `below_match_threshold`.
Przy jawnym dopuszczeniu różnicy stanu każdy stopień obniża składnik stanu
domyślnie o 25 punktów; nie stosujemy nieudokumentowanej korekty ceny.

## Dowody i próba

`PriceObservation` wymaga źródła, stabilnego ID, cech, kwoty Decimal, waluty,
czasu UTC, oceny wiarygodności źródła oraz oznaczenia demo. Ocena wiarygodności
jest metadaną zaufanego adaptera/polityki źródeł, nie polem pochodzącym z opisu
sprzedającego. W MVP nie ma jeszcze produkcyjnego adaptera potwierdzonych sprzedaży.

Odrzucamy inną walutę, inną proweniencję demo/real, źródła poniżej progu
wiarygodności, przyszłe lub zbyt stare obserwacje, niedopasowane produkty i samą
wycenianą ofertę. Jedno `(source_name, external_id)` to jedna obserwacja:
identyczne duplikaty liczą się raz; sprzeczne wersje wykluczają cały identyfikator.
Rozpoznawanie tej samej oferty między różnymi źródłami wymaga przyszłego adaptera.

SOLD wymaga `sale_confirmed=true` i niepustej `evidence_reference` do dowodu
potwierdzonej ceny transakcyjnej. Samo `listing_disappeared=true` niczego nie
potwierdza. Po filtracji preferujemy wyłącznie wiarygodne SOLD. Jeśli pozostaje
choć jedno SOLD, zbyt małej próby nie uzupełniamy cenami ASKING. Gdy brak
kwalifikujących się SOLD, wyceniamy z ASKING z oznaczeniem `asking_based`.
Niepotwierdzone SOLD są odrzucane, a nie przemianowywane na ASKING.

## Estymator i confidence

Domyślnie potrzeba 5 obserwacji. Kwartyle Q1/Q3 wyliczamy metodą liniową
Hyndmana–Fana typu 7: pozycja `(n−1) × p`, interpolacja wyłącznie na Decimal.
IQR = Q3−Q1. Jednorazowo odrzucamy ceny poza domkniętym przedziałem
`[Q1−1.5×IQR, Q3+1.5×IQR]`. Przy IQR=0 pozostają ceny równe kwartylom.
Ponownie sprawdzamy minimum, a następnie obliczamy medianę i kwartyle próbki
pozostałej po filtracji. `sample_size` oznacza właśnie tę końcową liczność.

`estimated_price` to mediana zaokrąglona `ROUND_HALF_UP` do skali waluty;
`median`, `q1`, `q3` zachowują dokładną precyzję statystyczną. Brak wystarczających
danych daje `status=INSUFFICIENT_DATA`, wszystkie cztery kwoty `null`, confidence
`none` i score 0. Nie zwracamy zmyślonej ceny ani zera jako zastępczej wyceny.

Confidence to jawna heurystyka, nie prawdopodobieństwo sprzedaży. Składniki
0–1 to: liczność `min(1,n/20)`, średni match/100, średnia wiarygodność źródeł
i `max(0,1−IQR/mediana)`. Wagi: 35%, 25%, 25%, 15%. Progi: medium od 0.60,
high od 0.85, poniżej low. ASKING oraz niezweryfikowany model mają domyślny
limit 0.59. Wynik zawiera źródła, referencje obserwacji, czas kalkulacji UTC,
wersję metody, odrzucenia i oznaczenie demo. To wycena, nie gwarancja zysku.

## Konfiguracja i cache

`app.config.PricingConfig` jest niemutowalny i walidowany. `Settings.pricing`
można skonfigurować zmienną `VDH_PRICING` zawierającą JSON, np.:

```sh
export VDH_PRICING='{"match_threshold":90,"min_observations":7,"cache_ttl_seconds":60}'
```

| Parametry | Domyślne wartości |
| --- | --- |
| `match_threshold`, `match_weights` | 80; name .45, brand .15, model .20, size .10, condition .10 |
| `max_condition_distance`, `condition_step_penalty` | 0; 25 |
| `min_observations`, `iqr_multiplier` | 5; 1.5 |
| `min_source_reliability`, `max_observation_age_days` | .8; 90 |
| `confidence_weights` | sample .35, match .25, reliability .25, dispersion .15 |
| `confidence_full_sample_size` | 20 |
| `confidence_medium_threshold`, `confidence_high_threshold` | .60; .85 |
| `asking_confidence_cap`, `missing_model_confidence_cap` | .59; .59 |
| `cache_ttl_seconds`, `cache_max_entries` | 300; 256 |

Wagi są względne i normalizowane przez ich sumę; suma musi być dodatnia.
Minimum próby można zmienić, ale nie poniżej 2. Zmiana polityki wymaga nowej
instancji estymatora. Cache LRU działa w pamięci jednej instancji, z blokadą
obsługującą współbieżność; TTL=0 go wyłącza. Klucz obejmuje znormalizowane cechy,
walutę, demo/real, tożsamość wyłączanej oferty, pełne dane obserwacji, ich aktualną
kwalifikowalność czasową i konfigurację. Kolejność obserwacji oraz `product_id`
nie wpływają na klucz. Hit zachowuje oryginalny `calculated_at`, ale zwraca
ID bieżącego produktu. Zmiana danych lub przekroczenie granicy świeżości
unieważnia wynik. Nie jest to cache pobierania danych ani trwały magazyn wycen.

## Przykład uruchomienia i testy

Po `uv sync --locked`, w katalogu repozytorium uruchom poniższy przykład.
Wszystkie dane i referencje w nim są jawnie fikcyjne:

```python
from datetime import UTC, datetime
from decimal import Decimal

from app.core.price_estimator import PriceEstimator
from app.schemas.pricing import PriceObservation, PriceType, PricingProduct, ProductFeatures
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.models import Condition

now = datetime.now(UTC)
features = ProductFeatures(
    product_name="DEMO shoe",
    brand="DEMO brand",
    model="DEMO model",
    size="EU 42",
    condition=Condition.GOOD,
)
product = PricingProduct(product_id="DEMO-product", features=features, currency="PLN", is_demo=True)
observations = [
    PriceObservation(
        source_name="demo-market",
        external_id=f"DEMO-{i}",
        features=features,
        price=Decimal(price),
        currency="PLN",
        price_type=PriceType.SOLD,
        observed_at=now,
        source_reliability=0.9,
        is_demo=True,
        sale_confirmed=True,
        evidence_reference=f"DEMO-fictional-receipt-{i}",
    )
    for i, price in enumerate(["90", "95", "100", "105", "110"])
]
result = PriceEstimator(Settings.from_env().pricing).estimate(product, observations)
print(result.model_dump_json(indent=2))
```

Kod uruchom w interpreterze `uv run --frozen python` lub zapisz do pliku i użyj
`uv run --frozen python plik.py`. Przy domyślnej konfiguracji wynik to 100 PLN,
5 obserwacji, podstawa SOLD i confidence medium. Testy modułów:
`uv run --frozen pytest tests/pricing`. Pełne kontrole opisuje README.

`PricingProduct.from_offer()` i `PriceObservation.from_offer()` integrują etap
importu z wyceną. Konwersja ceny ofertowej zawsze daje ASKING; czas obserwacji
i wiarygodność trzeba podać jawnie. Import nie zawiera pola modelu, więc model
pozostaje nieznany, chyba że wywołujący dostarczy zweryfikowane uzupełnienie.
Ten etap nie dodaje endpointu wyceny, historii transakcji ani migracji bazy.
