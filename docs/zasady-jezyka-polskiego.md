# Zasady języka polskiego w aplikacji

Ta reguła obowiązuje przy każdej kolejnej zmianie interfejsu i dokumentacji wyświetlanej przez aplikację.

## Redakcja

Tekst powinien być poprawny gramatycznie, stylistycznie, interpunkcyjnie i ortograficznie. Pisz naturalnie, płynnie i profesjonalnie, ale prostymi zdaniami. Uporządkuj treść w logiczne sekcje. Usuń zbędne powtórzenia i niezręczne kalki językowe. Komunikat błędu powinien wyjaśniać problem i wskazywać możliwy następny krok.

## Styl techniczny

1. Pisz krótko i rzeczowo. Pomijaj zbędne wstępy, podsumowania i zwroty takie jak „warto zauważyć”, „należy pamiętać” czy „kluczowym elementem jest”.
2. Nie rozdzielaj części zdania pauzą ani półpauzą. Użyj kropki, przecinka, dwukropka, średnika lub nawiasu. Zakres w zdaniu zapisuj jako „od ... do ...”.
3. W dokumentacji stosuj listy numerowane lub znak •, bez list rozpoczynanych łącznikiem.
4. Reguła nie zmienia składni kodu, poleceń, nazw własnych, danych źródłowych ani istniejących archiwów. Samodzielny znak braku danych w liczniku pozostaje oznaczeniem braku wartości, a nie zerem.
5. Skracanie tekstu nie może zmieniać jego znaczenia. Nie zastępuj planowanej przerwy określeniem sugerującym pomiar prądu, a dopasowania komunikatu potwierdzeniem braku zasilania.

## Spójna terminologia

| Pojęcie | Zalecana forma |
| --- | --- |
| backend w tekście dla użytkownika | część serwerowa |
| endpoint | punkt końcowy API |
| snapshot | migawka |
| cache | pamięć podręczna |
| backup | kopia zapasowa |
| restore | odtworzenie z kopii |
| retry | ponowienie próby |
| timeout | przekroczenie czasu oczekiwania; limit czasu, jeśli chodzi o ustawienie |
| reverse geocoding | geokodowanie odwrotne |
| payload | treść wiadomości lub dane odpowiedzi, zależnie od kontekstu |
| popup mapy | okno opisu na mapie |
| worker powiadomień | mechanizm wysyłki powiadomień |
| stan źródła | aktualność i wynik odczytu danych operatora |

„Potwierdzone objęcie punktu” oznacza dowód dotyczący komunikatu operatora. Nie oznacza pomiaru napięcia. „Trwające” odnosi się do harmonogramu komunikatu, a nie do potwierdzonego braku zasilania.

## Terminologia elektroenergetyczna

| Pojęcie | Znaczenie i zastosowanie w projekcie |
| --- | --- |
| OSD | Operator systemu dystrybucyjnego; odpowiada za sieć dystrybucyjną. Nie jest synonimem sprzedawcy energii. |
| OSP | Operator systemu przesyłowego. Warstwa sieci PSE nie jest komunikatem OSD ani dowodem przyłączenia obiektu. |
| Obiekt | Monitorowana lokalizacja w katalogu aplikacji. Jej identyfikator nie jest numerem PPE ani numerem licznika. |
| Punkt obiektu | Współrzędne zapisane w katalogu. Same współrzędne nie identyfikują przyłącza ani PPE. |
| PPE | Punkt poboru energii. Nie wyznaczamy go automatycznie z adresu, nazwy ani punktu na mapie. |
| Licznik | Przyrząd pomiarowy; jego numer nie jest numerem PPE. Aplikacja nie odczytuje liczników energii elektrycznej. |
| Przyłącze | Element fizycznego połączenia instalacji z siecią. Model dodatkowego toru zasilania nie potwierdza osobnego przyłącza ani niezależnego OSD. |
| Dodatkowy tor zasilania | Jedna spójna etykieta pola `secondary_feed`. Opisuje zadeklarowane wyposażenie; nie określa topologii sieci ani aktualnej dostępności zasilania. |
| SZR | Samoczynne załączanie rezerwy. Układ SZR i agregat są elementami wyposażenia, których działanie wymaga osobnego potwierdzenia. |
| Awaria | Typ komunikatu operatora. Dopasowanie takiego komunikatu nie dowodzi braku zasilania monitorowanego obiektu. |
| Planowane wyłączenie | Krótka etykieta planowanej przerwy w dostarczaniu energii elektrycznej. Nie zastępuje komunikatu o awarii ani potwierdzenia faktycznego odłączenia. |
| Moc i energia | Wielkości odrębne. Symbole mocy: W, kW, MW; energii: Wh, kWh, MWh.  |
| Napięcie i prąd | Wielkości odrębne: napięcie podajemy w V lub kV, natężenie prądu w A. Aplikacja nie mierzy tych wielkości; stosujemy określenie „stan zasilania”, gdy mowa o ogólnej sytuacji obiektu. |

Podstawy terminologii: [URE: dostarczanie energii i rola OSD](https://www.ure.gov.pl/pl/konsumenci/rachunki-pod-kontrola-konsumen/dostarczanie-energii-do-gospod/12780%2Cdok.html), [PSE: pytania, odpowiedzi i słownik](https://www.pse.pl/oire/qa-i-slownik), [TAURON Dystrybucja: numer PPE](https://www.tauron-dystrybucja.pl/uslugi-dystrybucyjne/liczniki-i-instalacje/numer-ppe), [Apator: układy SZR](https://www.apator.com/produkty-i-uslugi/energia-elektryczna/rozdzial-energii/samoczynne-zalaczanie-rezerwy-szr). Zweryfikowano 19.10.2026. Definicje odnosimy do modelu aplikacji; nie potwierdzają wyposażenia ani przyłączeń konkretnych obiektów.

## Daty, liczby i jednostki

Daty w interfejsie wyświetlamy po polsku, w strefie `Europe/Warsaw`; wspólny zapis zawiera dzień, miesiąc, rok, godzinę i minuty. Techniczne daty API, logów i danych źródłowych zachowują wymagany format i strefę. Brak daty opisujemy jako brak danych, bez podstawiania bieżącego czasu.

W liczbach prezentowanych przez aplikację stosujemy przecinek dziesiętny i odstęp przed symbolem jednostki, np. „2,5 kW”. Zachowujemy oryginalną precyzję, jednostki i wartości danych operatora. Współrzędne przeznaczone do skopiowania, identyfikatory i przykłady API zachowują zapis techniczny. Nie zmieniamy nazw pobranych ze źródła.

## Elementy chronione

Zachowuj wszystkie informacje techniczne. Nie zmieniaj poleceń terminala, bloków kodu, adresów API, odnośników, parametrów, identyfikatorów ani nazw pól. Nazwy bibliotek, standardów i protokołów, np. Python, aiohttp, SQLite, JSON, HTTP, TERYT, SIMC, ULIC i SZR, zachowują oryginalną postać.

Nie poprawiaj cudzych danych: komunikatów operatorów, nazw i adresów obiektów, uwag eksploatacyjnych użytkownika, oryginalnych logów i archiwalnych rekordów. Redakcja dotyczy tekstów napisanych przez projekt. Oryginalna treść może być pokazana jako dane źródłowe lub szczegóły techniczne.

## Liczniki i dostępność

Stosuj poprawną odmianę: „1 obiekt”, „2 obiekty”, „5 obiektów”, „12 obiektów”, „22 obiekty”. Nie utożsamiaj braku danych z zerem. Podpowiedzi i etykiety dla czytników ekranu również muszą być po polsku.

## Kontrola przed publikacją

Sprawdź zwykły widok, brak danych, błąd, ładowanie i rozwijane szczegóły. Obejrzyj interfejs na telefonie i komputerze. Przy redakcji instrukcji porównaj bloki kodu, odnośniki i wartości techniczne z wersją sprzed zmian. Tłumaczenia Homera stosuj podczas budowania przypiętej wersji; konfiguracja tłumaczeń powinna wykrywać zmianę oczekiwanych tekstów.

## Nagłówki i nawigacja

Panel, README i SNUT zachowują wspólny nagłówek, nazwy i kolejność zakładek. Styl pochodzi z `web/app-header.css`. Tytuł otwartej strony należy do jej treści. Aktywny widok oznaczamy przez `aria-current="page"` i pokazujemy także na przewijanym pasku w telefonie.
