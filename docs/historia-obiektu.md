# Historia obiektu

Po kliknięciu obiektu na mapie wybierz zakładkę **Historia**. Ta sama zakładka jest dostępna przy każdym obiekcie w katalogu.

Widok pokazuje maksymalnie trzy różne komunikaty dotyczące tego obiektu. Każdy komunikat pojawia się raz, w wersji z najnowszego dostępnego zapisu. Lista zaczyna się od komunikatu, który aplikacja zapisała jako ostatni.

Wpis zawiera rodzaj komunikatu (np. awaria, awaria masowa lub planowane wyłączenie), operatora, datę zapisu, rodzaj zmiany, podane terminy i opis. Data zapisu jest odrębna od terminu zdarzenia. Wpis może informować także o odwołaniu komunikatu, zakończeniu jego publikacji lub utracie dopasowania do obiektu.

Historia obejmuje zapisy dostępne w aplikacji. Pusta lista nie pozwala wnioskować o wcześniejszych awariach ani o stanie zasilania. Dopasowanie komunikatu nie zastępuje pomiaru napięcia. Informacje o wysyłce powiadomień push należą do osobnej historii.

Dostęp do historii zależy od aktualnej polityki publikacji. Wersja demonstracyjna oznacza wpisy jako symulację. Aplikacja pobiera historię po wybraniu zakładki. Jeśli odczyt się nie powiedzie, wybierz **Ponów próbę**. Między zakładkami możesz przechodzić strzałkami oraz klawiszami Home i End.

## API

Przykład odczytu (zastąp `ID_OBIEKTU` identyfikatorem wybranej lokalizacji):

```text
/api/v1/history?object_id=ID_OBIEKTU&limit=3&latest_per_event=1
```

Po ustawieniu `latest_per_event=1` API wybiera ostatni dostępny wpis każdego komunikatu, a potem dzieli wyniki na strony. Pole `total` podaje wtedy liczbę różnych komunikatów. Bez tego parametru API zwraca wszystkie zapisane zmiany. O kolejności decyduje zapis w archiwum. Planowany początek przerwy nie wpływa na kolejność. Po zmianie migawki zacznij od pierwszej strony.

Funkcja korzysta z istniejącego archiwum SQLite i nie wymaga migracji danych. Historia nie obejmuje zdarzeń, których aplikacja nie zapisała.
