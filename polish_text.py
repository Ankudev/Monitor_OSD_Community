"""Polska odmiana rzeczowników w komunikatach tworzonych przez aplikację."""


def count(value: int | None, forms):
    """Zwraca liczbę i jedną z form: pojedyncza, mnoga 2–4, mnoga 5+."""
    if value is None:
        return f"— {forms[2]}"
    if value == 1:
        form = forms[0]
    elif isinstance(value, int) and 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{value} {form}"
