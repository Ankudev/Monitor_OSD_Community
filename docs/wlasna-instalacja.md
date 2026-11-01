# Własna instalacja

Demo uruchom według README. Do własnych danych przygotuj oddzielny, pusty katalog:

```bash
python tools/init_local.py --data-dir .runtime/local
python tools/set_admin_password.py --help
python app.py --profile local --data-dir .runtime/local
```

Inicjalizator tworzy pusty katalog lokalizacji i pusty schemat słowników. Nie pobiera urzędowego rejestru. Przed dodaniem rzeczywistych lokalizacji należy przygotować `data/teryt.db` z tabelami `simc(sym,nazwa,woj,pow,gmi)` i `ulic(sym_ul,sym,cecha,nazwa_1,nazwa_2)` oraz udokumentować źródło danych. Nie zastępuj kodów urzędowych kodami demonstracyjnymi.

Konfiguracja administratora używa `PGE_ADMIN_PASSWORD_FILE` wskazującego plik skrótu hasła i `PGE_PUBLIC_ORIGIN` określającego dokładny adres HTTPS. Hasło utwórz interaktywnym narzędziem, zgodnie z jego pomocą. Plik ze skrótem zachowaj poza repozytorium z ograniczonymi uprawnieniami. Dla lokalnego HTTP na pętli zwrotnej można ustawić `PGE_ADMIN_LOCAL_HTTP=1`; nie stosuj tego ustawienia na publicznym serwerze.

Po zalogowaniu panel polityki jest pod `/api/admin/publication/setup`. Nowe lokalizacje są prywatne. Wymagane są osobne decyzje dla źródeł i konkretnych wartości pól; brak decyzji zamyka publikację. Własna decyzja nie zastępuje praw do danych.

Profil `local` nie uruchamia harmonogramu skanowania. Profil `live` wymaga zmiany profilu konfiguracji, rzeczywistych lokalizacji i słowników, jawnej listy `enabled_sources` oraz decyzji zezwalających na pobieranie i przechowywanie danych. Sprawdź warunki źródeł przed ich aktywacją. Domyślnie wysyłka pozostaje wyłączona. Ta część wersji alpha wymaga własnego odbioru na docelowych danych.

## Serwer

Wzór jednostki: `deploy/community/monitor-osd-community.service`. Używa osobnego użytkownika, portu 8082, katalogu danych i środowiska Python. Przed startem utwórz konto usługi, środowisko i demo w docelowym katalogu; nadaj mu prawo zapisu wyłącznie do jego danych. Sprawdź dostęp do interpretera jako użytkownik usługi. Katalog kodu powinien pozostać tylko do odczytu.

Domena demo musi wskazywać tę usługę. Ustaw publiczny adres w osobnym pliku środowiska; nie dodawaj haseł ani tokenów do Git. Monitoring gotowości powinien wymagać JSON `status = ready` pod `/health/ready`.

## Kopie i aktualizacje

Własną instalację kopiuj spójnie po zatrzymaniu usługi albo przez chronione API migawek. `service_backup.py` definiuje dozwolone pliki Community. Kopia obejmuje także politykę publikacji. Odtwarzaj wyłącznie do nowego katalogu i sprawdź działanie przed przełączeniem.

Wydanie instaluj w nowym katalogu `releases/<wersja>`, sprawdź sumy SHA-256 i przełącz `current` po odbiorze. Przed migracją schematu zachowaj kopię. Powrót do starego kodu wymaga zgodnego schematu lub odtworzenia kopii. Nie uruchamiaj starej wersji na niezgodnej bazie.
