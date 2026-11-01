"""Powiadomienia do jawnie wskazanych subskrypcji, bez wysyłki do segmentów."""
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

import aiohttp
from aiohttp import web
from service_backup import no_symlinks

ENDPOINT='https://api.onesignal.com/notifications'
ACTIONS={'new':'Nowy komunikat','updated':'Aktualizacja komunikatu',
         'resolved':'Zakończenie komunikatu','cancelled':'Odwołanie komunikatu',
         'not_published':'Brak komunikatu w ostatnim odczycie',
         'out_of_scope':'Komunikat poza zakresem obiektu'}


def identifier(value):
    if not isinstance(value,str) or str(uuid.UUID(value)) != value:
        raise ValueError('Niepoprawny identyfikator powiadomień.')
    return value


def configuration():
    application=identifier(os.environ.get('PGE_ONESIGNAL_APP_ID'))
    key_path=no_symlinks(os.environ['PGE_ONESIGNAL_API_KEY_FILE'])
    recipients_path=no_symlinks(os.environ['PGE_ONESIGNAL_RECIPIENTS_FILE'])
    if key_path.stat().st_size > 4096 or recipients_path.stat().st_size > 8192:
        raise ValueError('Konfiguracja powiadomień przekracza limit.')
    key=key_path.read_text(encoding='ascii').strip()
    if not key.startswith('os_v2_app_') or not 32 <= len(key) <= 4096 or not all(33 <= ord(c) <= 126 for c in key):
        raise ValueError('Niepoprawny klucz powiadomień.')
    recipients=json.loads(recipients_path.read_text(encoding='utf8'))
    if not isinstance(recipients,list) or not 1 <= len(recipients) <= 20:
        raise ValueError('Wskaż od 1 do 20 subskrypcji odbiorcy.')
    return application,key,list(dict.fromkeys(identifier(value) for value in recipients))


async def send_message(session,title,message,delivery_id,*,now=None):
    """HTTP 2xx bez identyfikatora wiadomości nie oznacza przyjęcia."""
    request_started=False
    try:
        application,key,recipients=await asyncio.to_thread(configuration)
        delivery_id=identifier(delivery_id)
        if not isinstance(title,str) or not isinstance(message,str) or not title or not message:
            raise ValueError('Brak treści powiadomienia.')
        payload=dict(app_id=application,target_channel='push',include_subscription_ids=recipients,
                     headings={'pl':title[:128],'en':title[:128]},
                     contents={'pl':message[:1000],'en':message[:1000]},
                     idempotency_key=delivery_id,name='Monitor OSD '+delivery_id,ttl=86400)
        request_started=True
        async with session.post(ENDPOINT,json=payload,headers={'Authorization':'Key '+key},
                                timeout=aiohttp.ClientTimeout(total=30),allow_redirects=False) as response:
            status=response.status
            if not 200 <= status < 300:
                from notification_queue import retry_after
                return dict(status='pending' if status in (408,429) or status>=500 else 'failed',
                            http_status=status,code='onesignal_http_'+str(status),
                            error='OneSignal zwrócił HTTP '+str(status),
                            retry_after=retry_after(response.headers.get('Retry-After'),time.time() if now is None else now))
            body=bytearray()
            while chunk:=await response.content.read(16384):
                body.extend(chunk)
                if len(body)>65536: raise ValueError('Odpowiedź przekracza limit.')
            result=json.loads(body)
            accepted=result.get('id') if isinstance(result,dict) else None
            if not accepted:
                return dict(status='failed',http_status=status,code='onesignal_no_recipients',
                            error='OneSignal nie utworzył wiadomości. Sprawdź subskrypcję odbiorcy.')
            identifier(accepted)
            if result.get('errors'):
                return dict(status='failed',http_status=status,code='onesignal_partial',
                            error='OneSignal zgłosił niepełną listę odbiorców.',provider_id=accepted)
            return dict(status='delivered',http_status=status,provider_id=accepted)
    except (aiohttp.ClientError,TimeoutError):
        return dict(status='pending',code='onesignal_transport',error='Nie można potwierdzić przyjęcia przez OneSignal.')
    except (KeyError,OSError,ValueError,TypeError):
        return dict(status='pending' if request_started else 'failed',
                    code='onesignal_response' if request_started else 'onesignal_configuration',
                    error='Sprawdź konfigurację OneSignal i odpowiedź dostawcy.')


async def deliver(session,job,now=None):
    moment=time.time() if now is None else now
    if job['destination_url'] != ENDPOINT:
        return dict(status='failed',code='onesignal_destination',error='Adres dostawy nie odpowiada konfiguracji OneSignal.')
    if moment-job['created_at']>29*86400:
        return dict(status='failed',code='onesignal_expired',error='Zadanie wymaga decyzji administratora po upływie okresu deduplikacji.')
    try:
        payload=json.loads(job['payload_json'])
        action=ACTIONS[payload['action']]
        name=str(payload.get('configured_name') or payload.get('object_id') or 'Monitorowany obiekt')[:120]
        operator=str(payload.get('operator') or payload.get('source') or 'OSD')[:80]
    except (KeyError,TypeError,ValueError):
        return dict(status='failed',code='onesignal_payload',error='Zadanie ma niepoprawną treść powiadomienia.')
    return await send_message(session,action,name+'. '+operator+'. Komunikat operatora nie jest pomiarem zasilania.',job['job_id'],now=moment)


def register(app,root):
    async def setup(request):
        return web.FileResponse(Path(root)/'web/admin-push.html')
    async def config(request):
        site_origin = os.environ.get('PGE_ONESIGNAL_SITE_ORIGIN', '').rstrip('/')
        public_origin = os.environ.get('PGE_PUBLIC_ORIGIN', '').rstrip('/')
        if site_origin and public_origin and site_origin != public_origin:
            return web.json_response({'error': 'Powiadomienia wymagają konfiguracji OneSignal dla nowego adresu i ponownego zapisania urządzenia.'}, status=503)
        try: application=identifier(os.environ.get('PGE_ONESIGNAL_APP_ID'))
        except (ValueError,TypeError): return web.json_response({'error':'Powiadomienia push nie są skonfigurowane.'},status=503)
        return web.json_response({'app_id':application})
    async def worker(request):
        return web.FileResponse(Path(root)/'web/OneSignalSDKWorker.js')
    app.router.add_get('/api/admin/push/setup',setup)
    app.router.add_get('/api/admin/push/config',config)
    app.router.add_get('/osd-push/OneSignalSDKWorker.js',worker)
