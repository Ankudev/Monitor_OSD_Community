# Historia obiektu

Po kliknięciu obiektu na mapie wybierz zakładkę **Historia**. Ta sama zakładka jest dostępna przy każdym obiekcie w katalogu.

Widok pokazuje maksymalnie trzy różne komunikaty dotyczące tego obiektu, uporządkowane od ostatnio zapisanego w archiwum. Dla każdego komunikatu widoczna jest jego najnowsza dostępna zmiana. Kolejne aktualizacje jednego komunikatu nie zajmują pozostałych miejsc. Gdy dostępnych jest mniej zapisów, lista jest krótsza.

Wpis zawiera rodzaj komunikatu (np. awaria, awaria masowa lub planowane wyłączenie), operatora, datę zapisu, rodzaj zmiany, podane terminy i opis. Data zapisu jest odrębna od terminu zdarzenia. Wpis może informować także o odwołaniu komunikatu, zakończeniu jego publikacji lub utracie dopasowania do obiektu.

Historia obejmuje dostępne zapisy aplikacji. Brak wpisów nie potwierdza braku wcześniejszych awarii ani prawidłowego zasilania. Dopasowanie komunikatu nie jest pomiarem zasilania. Widok nie jest historią dostarczenia powiadomień push.

Dostęp do historii respektuje aktualną politykę publikacji. Wersja demonstracyjna oznacza wpisy jako symulację. Historia jest pobierana po wybraniu zakładki; błąd odczytu umożliwia ponowienie próby. Zakładki obsługują klawisze strzałek, Home i End.

## API

Przykład odczytu (zastąp `ID_OBIEKTU` identyfikatorem wybranej lokalizacji):

```text
/api/v1/history?object_id=ID_OBIEKTU&limit=3&latest_per_event=1
```

Parametr `latest_per_event=1` wybiera po jednym ostatnim dostępnym wpisie każdego komunikatu przed paginacją. `total` oznacza w tym trybie liczbę różnych komunikatów. Bez parametru API zachowuje wcześniejsze działanie: zwraca wszystkie zapisane zmiany. Kolejność wynika z kolejności zapisu w archiwum, a nie z planowanego początku przerwy. W razie zmiany migawki należy rozpocząć odczyt od pierwszej strony.

Zmiana nie wymaga migracji danych. Wykorzystuje istniejące archiwum SQLite; nie odtwarza wstecz zdarzeń, których aplikacja nie zapisała.
