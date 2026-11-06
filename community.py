"""Profile Community i granica dostępu publicznego demo."""
import csv
import io
import json
import asyncio
import html

from aiohttp import web

NOTICE = 'Dane demonstracyjne. Nie są rzeczywistymi komunikatami o przerwach w zasilaniu.'
PROFILES = ('demo', 'local', 'live')


async def html_response(path, mode, *, headers=None):
    """Oznaczenie demo dociera razem ze stroną, także bez JavaScriptu i API."""
    if mode != 'demo':
        return web.FileResponse(path, headers=headers)
    text = await asyncio.to_thread(path.read_text, encoding='utf-8')
    note = ('<aside id="community-demo-notice" role="note">' + html.escape(NOTICE)
            + ' Punkty miejscowości są orientacyjne; przypisania operatorów należą do scenariusza.</aside>')
    if '<body>' in text:
        text = text.replace('<body>', '<body class="community-demo">' + note, 1)
    else:
        text = text.replace('<h1>', note + '<h1>', 1)
    return web.Response(text=text, content_type='text/html', headers=headers)


def profile(value):
    if value not in PROFILES:
        raise ValueError('Nieznany profil. Wybierz demo, local albo live.')
    return value


def validate_demo_config(config):
    if config['notifications_enabled'] or config['our_api_url'] or config['geoportal_enabled']:
        raise ValueError('Demo wymaga wyłączonej wysyłki i usług lokalizacyjnych.')


def guard(mode):
    profile(mode)

    @web.middleware
    async def middleware(request, handler):
        blocked = request.path.startswith(('/api/admin/', '/api/location/', '/osd-push/'))
        session_read = request.path == '/api/admin/session' and request.method in ('GET', 'HEAD')
        if mode == 'demo' and ((blocked and not session_read) or request.method not in ('GET', 'HEAD', 'OPTIONS') or request.query.get('include_raw') == '1'):
            response = web.json_response({'error': 'Demo udostępnia wyłącznie publiczny odczyt.'}, status=403)
        elif request.path == '/api/community':
            if request.method not in ('GET', 'HEAD'):
                raise web.HTTPMethodNotAllowed(request.method, ['GET', 'HEAD'])
            response = web.json_response({'name': 'Monitor OSD Community', 'version': '0.1.2-alpha',
                                      'profile': mode, 'demo': mode == 'demo',
                                      'notice': NOTICE if mode == 'demo' else '',
                                      'repository': 'https://github.com/Ankudev/Monitor_OSD_Community'},
                                     headers={'Cache-Control': 'no-store'})
        elif mode == 'demo' and session_read:
            response = web.json_response({'authenticated': False})
        else:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                if mode != 'demo':
                    raise
                response = error
        if mode != 'demo':
            return response
        response.headers['X-OSD-Data-Mode'] = 'demo'
        response.headers['Cache-Control'] = 'no-store'
        if request.path.startswith('/api/v1/'):
            from api_v1 import COMMON_HEADERS
            for key, value in COMMON_HEADERS.items():
                if key != 'Cache-Control':
                    response.headers.setdefault(key, value)
        if isinstance(response, web.Response) and response.body and request.path.startswith('/api/'):
            if response.content_type == 'application/json':
                payload = json.loads(response.body)
                if isinstance(payload, dict):
                    if request.path == '/api/v1/openapi.json' and 'openapi' in payload:
                        payload.update({'x-osd-demo': True, 'x-osd-notice': NOTICE})
                    else:
                        payload.update(demo=True, notice=NOTICE)
                elif isinstance(payload, list):
                    payload = [dict(row, demo=True, notice=NOTICE) if isinstance(row, dict) else row for row in payload]
                response.body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            elif response.content_type == 'text/csv':
                rows = list(csv.reader(io.StringIO(response.body.decode('utf-8-sig'))))
                stream = io.StringIO(newline='')
                writer = csv.writer(stream)
                if rows:
                    writer.writerow(rows[0] + ['data_mode', 'notice'])
                    writer.writerows(row + ['demo', NOTICE] for row in rows[1:])
                response.body = stream.getvalue().encode('utf-8-sig')
            response.headers.pop('ETag', None)
            response.headers['Cache-Control'] = 'no-store'
        return response

    return middleware
