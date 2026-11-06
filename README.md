# Monitor OSD Community

Rozwijany społecznościowo monitor komunikatów o awariach i planowanych przerwach w dostarczaniu energii elektrycznej. Silnik obsługuje formaty pięciu operatorów systemów dystrybucyjnych (OSD): PGE Dystrybucja, TAURON Dystrybucja, ENERGA-OPERATOR, ENEA Operator i Stoen Operator.

**Wersja `0.1.3-alpha` domyślnie działa w profilu demonstracyjnym i pokazuje zdarzenia symulowane.** Projekt nie jest oficjalnym serwisem operatorów. Dopasowanie komunikatu nie jest pomiarem napięcia ani potwierdzeniem braku zasilania. Nie podejmuj na tej podstawie decyzji dotyczących bezpieczeństwa pracy przy instalacji elektrycznej.

[Historia zmian](CHANGELOG.md) opisuje kolejne wydania.

## Co można zobaczyć

1. mapę i katalog 16 przykładowych miejscowości z całej Polski;
2. zdarzenia aktywne, planowane, zakończone i odwołane;
3. wyjaśnienie dopasowania komunikatu, jego ograniczeń i informacji o terminie;
4. historię, eksporty i publiczne API tylko do odczytu;
5. pulpit SNUT oparty na Homerze;
6. w samodzielnej instalacji także chronioną edycję katalogu i decyzje o publikacji poszczególnych pól.

W wersji demonstracyjnej sześć lokalizacji nie ma przypisanych zdarzeń. Pusty widok jest częścią scenariusza i nie potwierdza stanu zasilania. Kody miejscowości są syntetyczne, a punkty na mapie orientacyjne. Zasady i pochodzenie danych opisuje [scenariusz demo](examples/demo/README.md).

## Szybki start

Projekt wymaga Pythona w wersji od 3.11 do 3.13. Środowiskiem referencyjnym jest Python 3.12. Zainstaluj zależności w osobnym środowisku wirtualnym. Wersję demonstracyjną uruchomisz na dołączonych danych przykładowych, bez kluczy API i danych produkcyjnych.

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

Otwórz `http://127.0.0.1:8080/`. Mapa, katalog i API działają bez budowania SNUT. Serwer demonstracyjny nie pobiera danych operatorów, nie geokoduje adresów i nie wysyła powiadomień. Przeglądarka może pobierać kafelki od dostawców map. Gdy kafelki są niedostępne, nadal możesz korzystać z listy.

Uruchom inicjalizator raz dla danego katalogu. Aplikacja używa tego katalogu przy kolejnych uruchomieniach. Inicjalizator odmawia jego nadpisania. Aby przygotować nowy scenariusz, wskaż inny katalog:

```bash
python tools/init_demo.py --data-dir .runtime/drugie-demo
python app.py --profile demo --data-dir .runtime/drugie-demo --port 8081
```

Terminy scenariusza odnawiają się przy uruchomieniu i podczas pracy demo. Historia początkowa pozostaje oznaczona jako symulacja. Zmiana profilu wymaga osobnego katalogu danych.

## Profile i dane

| Profil | Zastosowanie |
| --- | --- |
| `demo` | Symulacja, publiczny odczyt, brak operacji administratora i wysyłki |
| `local` | Własny katalog i administracja, z wyłączonym harmonogramem odczytu źródeł |
| `live` | Jawnie uruchomione adaptery OSD z własną konfiguracją oraz decyzjami dotyczącymi źródeł |

Profile `local` i `live` wymagają przygotowania konfiguracji według [instrukcji własnej instalacji](docs/wlasna-instalacja.md). Zmiana wartości `profile` w pliku demonstracyjnym nie przenosi danych. Profil `live` odrzuca przykładowe identyfikatory.

Nowy obiekt jest prywatny. Publiczna mapa, API, historia i eksporty wymagają zatwierdzenia pól powiązanego z ich wartościami. Edycja pola unieważnia wcześniejsze zatwierdzenie. Status weryfikacji lokalizacji i zgoda na publikację są odrębnymi informacjami.

Ocena warunków wykorzystania rzeczywistych źródeł jest otwarta. Publiczna dostępność komunikatu nie zastępuje ustalenia zasad jego przechowywania i rozpowszechniania. Licencja projektu obejmuje jego kod i autorskie przykłady, nie dane operatorów.

## Pulpit SNUT

Do zbudowania pulpitu potrzebujesz Git, Node.js 22 i Corepack. Narzędzie pobiera określoną wersję Homera oraz wersje zależności zapisane w pliku blokady (lockfile).

```bash
python -m pip install -r requirements-dev.txt
npm install --global corepack@0.34.6
python tools/build_snut.py --release-id community-0.1.3-alpha
```

Po uruchomieniu aplikacji pulpit jest dostępny pod `/snut/`. Paczki wydań zawierają gotowy pulpit. Jeśli w instalacji ze źródeł pulpit nie został zbudowany, strona SNUT wyświetla komunikat o niedostępności. Mapa i API mogą nadal działać.

## Instalacja z paczki wydania

Pobierz archiwum `.tgz` z zakładki Releases repozytorium i porównaj jego SHA-256 z sumą podaną przy załączniku. Rozpakuj je do pustego katalogu. W tym katalogu utwórz środowisko Python, zainstaluj `requirements.txt`, uruchom `tools/init_demo.py`, a następnie `app.py`, używając poleceń z szybkiego startu. Pomiń klonowanie repozytorium. Paczka zawiera gotowy katalog `snut/`, który aplikacja wybiera automatycznie.

## API i kontrola stanu

Dokumentacja jest dostępna pod `/api-docs/`, a specyfikacja pod `/api/v1/openapi.json`. Dane możesz odczytać między innymi z `/api/v1/map`, `/api/v1/locations` i `/api/v1/history`. Kolejne strony pobieraj według odnośników zwracanych przez API. Po zmianie migawki może być konieczne rozpoczęcie od pierwszej strony.

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
