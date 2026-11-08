# Monitor OSD Community

Rozwijam Monitor OSD Community jako otwarty projekt do śledzenia komunikatów o awariach i planowanych przerwach w dostarczaniu energii elektrycznej. Silnik obsługuje formaty pięciu operatorów systemów dystrybucyjnych (OSD). Są to PGE Dystrybucja, TAURON Dystrybucja, ENERGA-OPERATOR, ENEA Operator i Stoen Operator.

**Wersja `0.1.3-alpha` domyślnie działa w profilu demonstracyjnym i pokazuje zdarzenia symulowane.** Projekt nie jest oficjalnym serwisem operatorów. Dopasowanie komunikatu nie jest pomiarem napięcia ani potwierdzeniem braku zasilania. Nie podejmuj na tej podstawie decyzji dotyczących bezpieczeństwa pracy przy instalacji elektrycznej.

Kolejne wydania opisuję w [historii zmian](CHANGELOG.md).

## Co można zobaczyć

1. Mapa i katalog 16 przykładowych miejscowości z całej Polski
2. Zdarzenia aktywne, planowane, zakończone i odwołane
3. Informacje o terminie oraz wyjaśnienie, jak dopasowano komunikat i jakie są ograniczenia tego dopasowania
4. Historia, eksporty i publiczne API tylko do odczytu
5. Pulpit SNUT oparty na Homerze
6. W samodzielnej instalacji także chroniona edycja katalogu i decyzje o publikacji poszczególnych pól

W scenariuszu demonstracyjnym pozostawiłem sześć lokalizacji bez przypisanych zdarzeń. Pusty widok jest częścią symulacji i nie potwierdza stanu zasilania. Użyłem syntetycznych kodów miejscowości i orientacyjnych punktów na mapie. Zasady i pochodzenie danych opisałem w [scenariuszu demo](examples/demo/README.md).

## Szybki start

Projekt wymaga Pythona w wersji od 3.11 do 3.13. Jako środowisko referencyjne przyjąłem Pythona 3.12. Zainstaluj zależności w osobnym środowisku wirtualnym. Wersję demonstracyjną uruchomisz na dołączonych danych przykładowych, bez kluczy API i danych produkcyjnych.

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

Otwórz `http://127.0.0.1:8080/`. Mapa, katalog i API działają bez budowania SNUT. Serwer demonstracyjny nie pobiera danych operatorów, nie geokoduje adresów i nie wysyła powiadomień. Przy wyświetlaniu mapy przeglądarka może pobierać kafelki bezpośrednio z serwerów OpenStreetMap i Geoportalu. Gdy kafelki są niedostępne, nadal możesz korzystać z listy.

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

Nowy obiekt jest prywatny. Aby udostępnić pole na publicznej mapie, w API, historii i eksportach, zatwierdź jego aktualną wartość. Edycja pola unieważnia wcześniejsze zatwierdzenie. Status weryfikacji lokalizacji i zgoda na publikację są odrębnymi informacjami.

Ocena warunków wykorzystania rzeczywistych źródeł pozostaje otwarta. Przed zapisaniem lub rozpowszechnieniem komunikatu ustal warunki korzystania z jego źródła, również gdy komunikat jest publicznie dostępny.

## Pulpit SNUT

Do zbudowania pulpitu potrzebujesz Gita, Node.js 22 i Corepacka. Narzędzie pobiera określoną wersję Homera oraz wersje zależności zapisane w pliku blokady (lockfile).

```bash
python -m pip install -r requirements-dev.txt
npm install --global corepack@0.34.6
python tools/build_snut.py --release-id community-0.1.3-alpha
```

Po uruchomieniu aplikacji pulpit jest dostępny pod `/snut/`. Paczki wydań zawierają gotowy pulpit. Jeśli w instalacji ze źródeł pulpit nie został zbudowany, strona SNUT wyświetla komunikat o niedostępności. Mapa i API mogą nadal działać.

## Instalacja z paczki wydania

Pobierz archiwum `.tgz` z zakładki Releases repozytorium. Porównaj sumę kontrolną SHA-256 archiwum z wartością podaną przy załączniku. Rozpakuj archiwum do pustego katalogu. W tym katalogu utwórz środowisko wirtualne Pythona, zainstaluj zależności z `requirements.txt`, uruchom `tools/init_demo.py`, a następnie `app.py`, używając poleceń z szybkiego startu. Pomiń klonowanie repozytorium. Paczka zawiera gotowy katalog `snut/`, który aplikacja wybiera automatycznie.

## API i kontrola stanu

Dokumentacja jest dostępna pod `/api-docs/`, a specyfikacja pod `/api/v1/openapi.json`. Dane możesz odczytać między innymi z `/api/v1/map`, `/api/v1/locations` i `/api/v1/history`. Kolejne strony pobieraj według odnośników zwracanych przez API. Po zmianie migawki może być konieczne rozpoczęcie od pierwszej strony.

Punkt końcowy `/api/community` podaje wersję i profil. Dane demonstracyjne mają oznaczenie w odpowiedziach JSON oraz w nagłówku `X-OSD-Data-Mode`. W specyfikacji OpenAPI służą do tego rozszerzenia `x-osd-demo` i `x-osd-notice`. Eksport CSV zawiera dodatkowe kolumny opisujące symulację. Sonda `/health/live` sprawdza działanie procesu, a `/health/ready` sprawdza gotowość magazynu i polityki publikacji. Dostęp do `/api/operations` wymaga osobnego tokenu.

## Rozwój i testy

```bash
python -m pip install -r requirements-dev.txt
python -m pip check
python tools/check_test_discovery.py
python -B -m unittest discover -s tests
python tools/render_readme.py --check
python tools/check_public_release.py
```

Aby wykonać testy JavaScript, uruchom `node` dla każdego pliku `tests/test_*.cjs`. Następnie wykonaj `node --test integrations/homer/tests/*.test.mjs`. Testy korzystają z własnych katalogów tymczasowych i syntetycznych odpowiedzi. Podczas testów nie uruchamiaj prawdziwych skanów ani powiadomień.

Zapraszam do [zgłaszania błędów i pomysłów](https://github.com/Ankudev/Monitor_OSD_Community/issues) oraz do przesyłania propozycji zmian (pull requestów). Zobacz [zasady współpracy](CONTRIBUTING.md), [plan rozwoju](ROADMAP.md) i [zasady zgłaszania podatności](SECURITY.md). Opisy funkcji, interfejs i dokumentację tworzę po polsku.

## Licencja

Własny kod i autorskie przykłady udostępniam na licencji [MIT](LICENSE). Dane operatorów podlegają warunkom ich źródeł. Licencje komponentów zewnętrznych podaję w [informacjach o zależnościach](THIRD_PARTY_NOTICES.md).
