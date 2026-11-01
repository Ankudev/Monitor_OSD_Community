"""Sesje administratora; brak konfiguracji zamyka dostęp do zapisu."""
import asyncio
import hmac
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path

from aiohttp import web
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from public_projection import public_data, has_public_response

KEY = web.AppKey('admin_auth', object)
PH = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


class AdminAuth:
    def __init__(self):
        self.sessions = {}
        self.attempts = deque(maxlen=100)
        self.lock = asyncio.Lock()
        self.origin = os.environ.get('PGE_PUBLIC_ORIGIN', '').rstrip('/')
        self.dev = os.environ.get('PGE_ADMIN_LOCAL_HTTP') == '1'
        self.cookie = 'osd_admin_dev' if self.dev else '__Host-osd_admin'
        try:
            self.password_hash = Path(os.environ['PGE_ADMIN_PASSWORD_FILE']).read_text().strip()
            PH.check_needs_rehash(self.password_hash)
        except (KeyError, OSError, InvalidHashError, ValueError):
            self.password_hash = None

    def session(self, request):
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if now-v['created'] < 28800 and now-v['used'] < 1800}
        return self.sessions.get(request.cookies.get(self.cookie, ''))

    def same_origin(self, request):
        expected = self.origin
        if self.dev and request.remote in ('127.0.0.1', '::1'):
            expected = f'{request.scheme}://{request.host}'
        return bool(expected and request.headers.get('Origin') == expected)

    async def endpoint(self, request):
        session = self.session(request)
        if request.path.endswith('/session'):
            return web.json_response({'authenticated': bool(session), **({'csrf_token':session['csrf']} if session else {})})
        if not self.same_origin(request):
            raise web.HTTPForbidden(text='Niedozwolone pochodzenie żądania.')
        if request.path.endswith('/logout'):
            self.require(request, session)
            self.sessions.pop(request.cookies.get(self.cookie, ''), None)
            response = web.json_response({'authenticated':False})
            response.del_cookie(self.cookie, path='/')
            return response
        if not self.password_hash:
            raise web.HTTPServiceUnavailable(text='Logowanie administratora nie jest skonfigurowane.')
        now = time.monotonic()
        while self.attempts and now-self.attempts[0][0] >= 900:
            self.attempts.popleft()
        # Adres bezpośredniego klienta; nie ufamy nagłówkom proxy.
        peer = request.remote
        if sum(t > now-60 for t, _ in self.attempts) >= 10 or sum(p == peer for _, p in self.attempts) >= 5:
            raise web.HTTPTooManyRequests(text='Zbyt wiele prób. Spróbuj później.', headers={'Retry-After':'900'})
        self.attempts.append((now, peer))
        if request.content_type != 'application/json' or request.content_length is None or request.content_length > 2048:
            raise web.HTTPBadRequest(text='Wymagany krótki dokument JSON.')
        try:
            password = (await request.json()).get('password')
        except (ValueError, UnicodeError, AttributeError):
            raise web.HTTPBadRequest(text='Niepoprawny JSON.') from None
        if not isinstance(password, str) or not 15 <= len(password) <= 128:
            raise web.HTTPUnauthorized(text='Nieprawidłowe hasło.')
        # Zajęte hashowanie nie tworzy nieograniczonej kolejki zadań.
        if self.lock.locked():
            raise web.HTTPTooManyRequests(text='Spróbuj ponownie.', headers={'Retry-After':'2'})
        try:
            async with self.lock:
                await asyncio.to_thread(PH.verify, self.password_hash, password)
        except (VerificationError, InvalidHashError):
            raise web.HTTPUnauthorized(text='Nieprawidłowe hasło.') from None
        self.attempts.remove((now, peer))
        self.sessions.pop(request.cookies.get(self.cookie, ''), None)
        if len(self.sessions) >= 20:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        session = {'csrf':secrets.token_urlsafe(32), 'created':now, 'used':now}
        self.sessions[token] = session
        response = web.json_response({'authenticated':True, 'csrf_token':session['csrf']})
        response.set_cookie(self.cookie, token, secure=not self.dev, httponly=True, samesite='Strict', path='/', max_age=28800)
        return response

    def require(self, request, session):
        if not session:
            raise web.HTTPUnauthorized(text='Zaloguj się do edycji.')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not self.same_origin(request) or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''), session['csrf']):
                raise web.HTTPForbidden(text='Sesja formularza jest nieprawidłowa. Zaloguj się ponownie.')
            session['used'] = time.monotonic()


@web.middleware
async def security_middleware(request, handler):
    auth = request.app[KEY]
    path = request.path
    private = path.startswith('/api/admin/') or path in ('/api/location/check','/api/location/resolve','/api/location/reverse') or path.startswith('/api/v1/diagnostics/') or path.endswith('/location-review') or request.query.get('include_raw') == '1'
    write = path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS')
    login_routes = path in ('/api/admin/login','/api/admin/session','/api/admin/logout')
    try:
        machine_backup = path == '/api/operations/snapshot' and request.method == 'POST'
        if (private or write) and not login_routes and not machine_backup:
            auth.require(request, auth.session(request))
        response = await handler(request)
    except web.HTTPException as error:
        if not path.startswith('/api/'):
            raise
        response = web.json_response({'error':error.text}, status=error.status, headers={k:v for k,v in error.headers.items() if k.lower() == 'retry-after'})
    if private or write or login_routes:
        response.headers['Cache-Control'] = 'no-store'
        for header in ('ETag','Access-Control-Allow-Origin'):
            response.headers.pop(header, None)
    elif (path.startswith('/api/') and response.content_type == 'application/json'
          and response.body and not has_public_response(response)):
        response.body = json.dumps(public_data(json.loads(response.body)), ensure_ascii=False).encode('utf-8')
    return response


def register(app):
    auth = AdminAuth()
    app[KEY] = auth
    for method, path in [('POST','login'),('GET','session'),('POST','logout')]:
        app.router.add_route(method, '/api/admin/'+path, auth.endpoint)
