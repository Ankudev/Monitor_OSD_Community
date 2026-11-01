"""Prawdziwe logowanie sesyjne dla testów operacji administratora."""
from admin_auth import KEY, PH

async def login_admin(case):
    auth = case.app[KEY]
    auth.dev = True
    auth.cookie = 'osd_admin_dev'
    auth.origin = str(case.client.make_url('/')).rstrip('/')
    auth.password_hash = PH.hash('Hasło wyłącznie do testów 2026')
    response = await case.client.post('/api/admin/login', json={'password':'Hasło wyłącznie do testów 2026'}, headers={'Origin':auth.origin})
    assert response.status == 200
    token = (await response.json())['csrf_token']
    case.client.session.headers.update({'Origin':auth.origin,'X-CSRF-Token':token})
