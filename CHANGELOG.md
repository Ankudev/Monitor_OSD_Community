# Historia zmian

## 0.1.3-alpha

Zapewniłem odczyt stanu silnika z jednej migawki SQLite. Równoległy zapis historii nie powoduje już pozornej niespójności archiwum. Dodałem osobne komunikaty diagnostyczne dla przekroczenia limitu czasu, błędu odczytu i niedostępności polityki publikacji.

## 0.1.2-alpha

Dodałem zakładkę **Historia** na mapie i w katalogu. Pokazuje maksymalnie trzy różne komunikaty z najnowszymi zapisami dotyczącymi wybranego obiektu. [Opis funkcji](docs/historia-obiektu.md).

## 0.1.1-alpha

[Naprawiłem scenariusz demonstracyjny i wydałem Community 0.1.1-alpha (#1)](https://github.com/Ankudev/Monitor_OSD_Community/commit/60146bb648e1ad185c69eb510fc71875f56b3023).

1. Poprawiłem typy i stany zdarzeń demonstracyjnych oraz objaśnienia dopasowania. Planowane przykłady trafiają do właściwych filtrów. Zachowałem wcześniejsze wpisy historii.
2. Dodałem oznaczenie symulacji bezpośrednio w HTML, aby było widoczne także bez odpowiedzi API profilu. Dostosowałem zegary i opisy źródeł do trybu demonstracyjnego w panelu i SNUT.
3. Wyłączyłem filtrowanie dokumentu OpenAPI według reguł dla danych katalogu. Oznaczenie wersji demonstracyjnej zapisałem w rozszerzeniach specyfikacji.
4. Dodałem automatyczne wykrywanie pulpitu SNUT dołączonego do paczki. W narzędziu pakowania zablokowałem nadpisywanie istniejących archiwów i umożliwiłem wskazanie osobnych ścieżek wejściowych oraz wynikowych.
5. Poprawiłem adres dokumentacji, opisy uprawnień i prywatnych pól, nazwy stron, etykiety ulubionych oraz kontrast informacji o źródłach.

## 0.1.0-alpha

Wydałem pierwszą wersję Community jako odrębny projekt. Przygotowałem mapę i katalog 16 miejscowości, syntetyczne zdarzenia dla pięciu adapterów, historię, eksporty i pulpit SNUT. Publiczną wersję demonstracyjną udostępniłem do odczytu, z wyłączoną administracją, geokodowaniem i wysyłaniem powiadomień.

Własne instalacje mają osobne katalogi danych, uwierzytelnianie i politykę publikacji pól. Rzeczywiste źródła i pełne słowniki wymagają konfiguracji oraz własnego odbioru. Punkt orientacyjny miejscowości nie jest pomiarem ani potwierdzonym punktem zasilania.
