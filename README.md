# Monitor OSD Community

Rozwijany społecznościowo monitor komunikatów o awariach i planowanych przerwach w dostarczaniu energii elektrycznej. Silnik obsługuje formaty pięciu operatorów systemów dystrybucyjnych (OSD): PGE Dystrybucja, TAURON Dystrybucja, ENERGA-OPERATOR, ENEA Operator i Stoen Operator.

**Wersja `0.1.1-alpha`. Domyślny profil to demo ze zdarzeniami symulowanymi.** Projekt nie jest oficjalnym serwisem operatorów. Dopasowanie komunikatu nie jest pomiarem napięcia ani potwierdzeniem braku zasilania. Nie służy do podejmowania decyzji o bezpieczeństwie pracy przy instalacji elektrycznej.

## Co można zobaczyć

1. mapę i katalog 16 przykładowych miejscowości z całej Polski;
2. zdarzenia aktywne, planowane, zakończone i odwołane;
3. oddzielne informacje o dopasowaniu, jego ograniczeniach i stanie terminu;
4. historię, eksporty i publiczne API tylko do odczytu;
5. pulpit SNUT oparty na Homerze;
6. we własnej instalacji: chronioną edycję katalogu i decyzje publikacji poszczególnych pól.

W demo sześć lokalizacji nie ma zdarzeń. To przykład pustego widoku, a nie informacja o rzeczywistym zasilaniu. Kody miejscowości są syntetyczne, a punkty na mapie orientacyjne. Zasady i pochodzenie danych opisuje [scenariusz demo](examples/demo/README.md).

## Szybki start

Wymagany Python w wersji od 3.11 do 3.13. Środowisko referencyjne: Python 3.12. Zależności instalujemy w osobnym środowisku. Nie są potrzebne klucze API ani dane produkcyjne.

Windows PowerShell:

```powershell
git clone https://github.com/Ankudev/Monitor_OSD_Community.git
cd Monitor_OSD_Community
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe tools/init_demo.py
.\.venv\Scripts\python.exe app.py
```

Linux:

```bash
git clone https://github.com/Ankudev/Monitor_OSD_Community.git
cd Monitor_OSD_Community
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/init_demo.py
.venv/bin/python app.py
```

Otwórz `http://127.0.0.1:8080/`. Mapa, katalog i API działają bez budowania SNUT. Część serwerowa demo nie pobiera źródeł operatorów, nie geokoduje i nie wysyła powiadomień. Przeglądarka może pobierać kafelki mapy; bez ich dostępności nadal można korzystać z listy. Dostawcy map otrzymują standardowe żądania przeglądarki.

Inicjalizację wykonaj raz. Kolejne uruchomienia używają tego samego katalogu; inicjalizator odmawia jego nadpisania. Nowy scenariusz przygotujesz, wskazując inny katalog:

```bash
python tools/init_demo.py --data-dir .runtime/drugie-demo
python app.py --profile demo --data-dir .runtime/drugie-demo --port 8081
```

Terminy scenariusza odnawiają się przy uruchomieniu i podczas pracy demo. Historia początkowa pozostaje oznaczona jako symulacja. Zmiana profilu wymaga osobnego katalogu danych.

## Profile i dane

| Profil | Zastosowanie |
| --- | --- |
| `demo` | Symulacja, publiczny odczyt, brak operacji administratora i wysyłki |
| `local` | Własny katalog i administracja; harmonogram odczytu źródeł wyłączony |
| `live` | Jawnie uruchomione adaptery OSD z własną konfiguracją oraz decyzjami dotyczącymi źródeł |

Profile `local` i `live` wymagają przygotowania konfiguracji według [instrukcji własnej instalacji](docs/wlasna-instalacja.md). Sama zmiana `profile` w pliku demo nie jest migracją danych: aplikacja odrzuca przykładowe identyfikatory w profilu `live`.

Nowy obiekt jest prywatny. Publiczna mapa, API, historia i eksporty wymagają zatwierdzenia pól powiązanego z ich wartościami. Edycja pola unieważnia wcześniejsze zatwierdzenie. Status weryfikacji lokalizacji i zgoda na publikację są odrębnymi informacjami.

Ocena warunków wykorzystania rzeczywistych źródeł jest otwarta. Publiczna dostępność komunikatu nie zastępuje ustalenia zasad jego przechowywania i rozpowszechniania. Licencja projektu obejmuje jego kod i autorskie przykłady, nie dane operatorów.

## Pulpit SNUT

Wymaga Git, Node.js 22 i Corepack. Budowanie pobiera przypiętą wersję Homera i zależności przypięte w pliku blokady wersji (lockfile).

```bash
python -m pip install -r requirements-dev.txt
npm install --global corepack@0.34.6
python tools/build_snut.py --release-id community-0.1.1-alpha
```

Po uruchomieniu aplikacji pulpit znajduje się pod `/snut/`. Gotowe paczki wydań zawierają zbudowany pulpit. Bez zbudowanego pulpitu strona SNUT zwraca informację o jego niedostępności; nie oznacza to awarii mapy lub API.

## Instalacja z paczki wydania

Pobierz archiwum `.tgz` z zakładki Releases repozytorium i porównaj jego SHA-256 z sumą podaną przy załączniku. Rozpakuj je do pustego katalogu. W tym katalogu utwórz środowisko Python, zainstaluj `requirements.txt`, uruchom `tools/init_demo.py`, a następnie `app.py`, używając poleceń z szybkiego startu. Pomiń klonowanie repozytorium. Paczka zawiera gotowy katalog `snut/`, który aplikacja wybiera automatycznie.

## API i kontrola stanu

Dokumentacja: `/api-docs/`, specyfikacja: `/api/v1/openapi.json`. Przykładowe odczyty: `/api/v1/map`, `/api/v1/locations`, `/api/v1/history`. Paginacja używa odnośników zwracanych przez API; po zmianie migawki może być konieczne ponowienie odczytu.

`/api/community` wskazuje wersję i profil. Dane demo mają oznaczenie w JSON (w OpenAPI jako rozszerzenia `x-osd-demo` i `x-osd-notice`) oraz nagłówku `X-OSD-Data-Mode`; eksport CSV zawiera dodatkowe kolumny opisujące symulację. `/health/live` sprawdza proces, a `/health/ready` gotowość magazynu i polityki. `/api/operations` wymaga osobnego tokenu.

## Rozwój i testy

```bash
python -m pip install -r requirements-dev.txt
python -m pip check
python tools/check_test_discovery.py
python -B -m unittest discover -s tests
python tools/render_readme.py --check
python tools/check_public_release.py
```

Testy JavaScript: uruchom `node` dla każdego `tests/test_*.cjs`, a następnie `node --test integrations/homer/tests/*.test.mjs`. Testy korzystają z własnych katalogów tymczasowych i syntetycznych odpowiedzi. Podczas testów nie uruchamiaj prawdziwych skanów ani powiadomień.

Zapraszamy do [zgłoszeń](https://github.com/Ankudev/Monitor_OSD_Community/issues) i pull requestów. Zobacz [zasady współpracy](CONTRIBUTING.md), [plan rozwoju](ROADMAP.md) i [zgłaszanie podatności](SECURITY.md). Opisy funkcji, interfejs i dokumentację piszemy po polsku.

## Licencja

Własny kod i przykłady: [MIT](LICENSE). Komponenty zewnętrzne zachowują własne licencje: [informacje o zależnościach](THIRD_PARTY_NOTICES.md).
