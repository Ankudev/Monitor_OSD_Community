"""Jawne źródło syntetyczne dla starszych testów logiki niezwiązanej z publikacją.

Testy polityki używają prawdziwego magazynu i domyślnie zamkniętego create_app.
Ta atrapa nie jest pakowana ani odczytywana przez aplikację produkcyjną.
"""
from copy import deepcopy
from publication_policy import PolicyView


class FixtureView(PolicyView):
    enforced = False

    def __init__(self):
        super().__init__(0, {}, {})

    def allows(self, source, action='publish'):
        return True

    def public_object(self, obj):
        return deepcopy(obj)

    def event_allowed(self, event, objects, action='publish'):
        return True

    def history_allowed(self, *args):
        return True


class FixturePolicy:
    def snapshot(self):
        return FixtureView()


def create_test_app(*args, **kwargs):
    import app
    kwargs['publication_policy'] = FixturePolicy()
    return app.create_app(*args, **kwargs)


def source(url, *, approved=False, active=False, basis='Testowe źródło danych.'):
    from publication_policy import ACTIONS
    return dict(url=url, review_status='approved' if approved else 'pending',
                publication_mode='public' if active else 'restricted', basis=basis,
                interim_owner_decision=active, actions={a:active for a in ACTIONS})
