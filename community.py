"""Profile Community i granica dostępu publicznego demo."""
import csv
import io
import json

from aiohttp import web

NOTICE = 'Dane demonstracyjne — nie są rzeczywistymi komunikatami o przerwach w zasilaniu.'
PROFILES = ('demo', 'local', 'live')


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
        if request.path == '/api/community':
            return web.json_response({'name': 'Monitor OSD Community', 'version': '0.1.0-alpha',
                                      'profile': mode, 'demo': mode == 'demo',
                                      'notice': NOTICE if mode == 'demo' else '',
                                      'repository': 'https://github.com/Ankudev/Monitor_OSD_Community'})
        if mode != 'demo':
            return await handler(request)
        if request.path == '/api/admin/session' and request.method in ('GET', 'HEAD'):
            return web.json_response({'authenticated': False, 'demo': True, 'notice': NOTICE},
                                     headers={'Cache-Control': 'no-store'})
        blocked = request.path.startswith(('/api/admin/', '/api/location/', '/osd-push/'))
        if blocked or request.method not in ('GET', 'HEAD', 'OPTIONS') or request.query.get('include_raw') == '1':
            return web.json_response({'error': 'Demo udostępnia wyłącznie publiczny odczyt.',
                                      'demo': True, 'notice': NOTICE}, status=403,
                                     headers={'Cache-Control': 'no-store'})
        response = await handler(request)
        response.headers['X-OSD-Data-Mode'] = 'demo'
        if isinstance(response, web.Response) and response.body and request.path.startswith('/api/'):
            if response.content_type == 'application/json':
                payload = json.loads(response.body)
                if isinstance(payload, dict):
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
