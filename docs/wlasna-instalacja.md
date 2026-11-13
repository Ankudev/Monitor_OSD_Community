# Własna instalacja

Demo uruchom według README. Do własnych danych przygotuj oddzielny, pusty katalog:

```bash
python tools/init_local.py --data-dir .runtime/local
python tools/set_admin_password.py --help
python app.py --profile local --data-dir .runtime/local
```

Inicjalizator tworzy pusty katalog lokalizacji i pusty schemat słowników. Nie pobiera urzędowego rejestru. Przed dodaniem rzeczywistych lokalizacji należy przygotować `data/teryt.db` z tabelami `simc(sym,nazwa,woj,pow,gmi)` i `ulic(sym_ul,sym,cecha,nazwa_1,nazwa_2)` oraz udokumentować źródło danych. Nie zastępuj kodów urzędowych kodami demonstracyjnymi.

Ustaw `PGE_ADMIN_PASSWORD_FILE` na ścieżkę do pliku ze skrótem hasła, a `PGE_PUBLIC_ORIGIN` na dokładny adres HTTPS. Aby utworzyć plik ze skrótem, uruchom interaktywne narzędzie i postępuj zgodnie z jego instrukcją. Zapisz plik poza repozytorium i ogranicz dostęp do niego. Na potrzeby lokalnego HTTP na adresie pętli zwrotnej możesz ustawić `PGE_ADMIN_LOCAL_HTTP=1`. Na publicznym serwerze użyj HTTPS.

Po zalogowaniu otwórz panel polityki publikacji pod `/api/admin/publication/setup`. Nowe lokalizacje są prywatne. Zatwierdź osobno źródła i konkretne wartości pól. Dane bez wymaganych decyzji pozostają prywatne. Decyzja w panelu nie nadaje praw do danych.

Profil `local` nie uruchamia harmonogramu skanowania. Profil `live` wymaga zmiany profilu konfiguracji, rzeczywistych lokalizacji i słowników, jawnej listy `enabled_sources` oraz decyzji zezwalających na pobieranie i przechowywanie danych. Sprawdź warunki źródeł przed ich aktywacją. Domyślnie wysyłka pozostaje wyłączona. Ta część wersji alpha wymaga własnego odbioru na docelowych danych.

## Własny token PGE w profilu `live`

We własnej instalacji z Linuksem i systemd możesz podłączyć swój token do odczytu komunikatów PGE. Bez poświadczenia profil `live` korzysta z dotychczasowego odczytu. W obu trybach obowiązują decyzje dotyczące źródła w polityce publikacji. Profile `demo` i `local` nie używają poświadczenia PGE.

1. Przygotuj osobną usługę systemd uruchamiającą `app.py --profile live` z własnym katalogiem danych i osobnym kontem usługi. Wzór usługi demonstracyjnej pozostaw w instalacji demo.
2. Utwórz katalog sekretów i uruchom narzędzie z interaktywnego terminala:

   ```bash
   sudo install -d -o root -g root -m 0700 /etc/monitor-osd-community-live/secrets
   sudo .venv/bin/python -B tools/set_pge_api_token.py
   ```

   Narzędzie dwukrotnie poprosi o token bez wyświetlania znaków. Zapisze go w pliku dostępnym dla administratora. Token podawaj w tym polu, poza poleceniami i konfiguracją aplikacji.

3. Dodaj konfigurację z [wzoru poświadczenia](../deploy/community/pge-official-api.conf.example) do jednostki własnej instalacji, np. przez `sudo systemctl edit monitor-osd-community-live.service`. Po zapisaniu konfiguracji uruchom ponownie tę usługę. Systemd przekaże poświadczenie procesowi oraz przygotuje prywatny katalog budżetu.

Po restarcie następny odczyt PGE korzysta z tokenu. Odpowiedzi 401 i 403 wstrzymują odczyt do sprawdzenia poświadczenia. Aplikacja zachowuje ostatni poprawny wynik. Po poprawieniu tokenu i restarcie uruchom `tools/resume_pge_api.py` jako konto własnej usługi, żeby wznowić próby bez zerowania budżetu. Wymiana tokenu wymaga ponownego uruchomienia usługi.

Aby wrócić do dotychczasowego odczytu, usuń wpis `LoadCredential` dodany do własnej jednostki i uruchom ją ponownie. Zmiana dotyczy przyszłych odczytów. Jeśli to samo poświadczenie jest używane przez więcej niż jeden proces, wszystkie muszą korzystać z tego samego magazynu budżetu.

## Serwer

Wzór jednostki: `deploy/community/monitor-osd-community.service`. Używa osobnego użytkownika, portu 8082, katalogu danych i środowiska Python. Przed startem utwórz konto usługi, środowisko i demo w docelowym katalogu; nadaj mu prawo zapisu wyłącznie do jego danych. Sprawdź dostęp do interpretera jako użytkownik usługi. Katalog kodu powinien pozostać tylko do odczytu.

Domena demo musi wskazywać tę usługę. Ustaw publiczny adres w osobnym pliku środowiska; nie dodawaj haseł ani tokenów do Git. Monitoring gotowości powinien wymagać JSON `status = ready` pod `/health/ready`.

## Kopie i aktualizacje

Własną instalację kopiuj spójnie po zatrzymaniu usługi albo przez chronione API migawek. `service_backup.py` definiuje dozwolone pliki Community. Kopia obejmuje także politykę publikacji. Odtwarzaj wyłącznie do nowego katalogu i sprawdź działanie przed przełączeniem.

Wydanie instaluj w nowym katalogu `releases/<wersja>`, sprawdź sumy SHA-256 i przełącz `current` po odbiorze. Przed migracją schematu zachowaj kopię. Powrót do starego kodu wymaga zgodnego schematu lub odtworzenia kopii. Nie uruchamiaj starej wersji na niezgodnej bazie.
