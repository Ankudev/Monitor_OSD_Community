import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import onesignal_push as push
import notification_queue as queue
import test_app as fixtures


class Content:
    def __init__(self,body): self.body=body
    async def read(self,n):
        chunk,self.body=self.body[:n],self.body[n:]
        return chunk


class Reply:
    def __init__(self,status,body,headers=None):
        self.status,self.headers=status,headers or {}
        self.content=Content(json.dumps(body).encode())
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False


class Session:
    def __init__(self,reply): self.reply,self.calls=reply,[]
    def post(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        if isinstance(self.reply,BaseException): raise self.reply
        return self.reply


class OneSignalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name)
        self.key=root/'key';self.key.write_text('os_v2_app_'+'k'*64)
        self.recipient=str(uuid.uuid4());self.app_id=str(uuid.uuid4())
        self.recipients=root/'recipients.json';self.recipients.write_text(json.dumps([self.recipient]))
        env=patch.dict(os.environ,PGE_ONESIGNAL_APP_ID=self.app_id,
                       PGE_ONESIGNAL_API_KEY_FILE=str(self.key),PGE_ONESIGNAL_RECIPIENTS_FILE=str(self.recipients))
        env.start();self.addCleanup(env.stop)
        self.delivery=str(uuid.uuid4());self.provider_id=str(uuid.uuid4())

    async def test_only_explicit_recipients_and_stable_idempotency_are_sent(self):
        session=Session(Reply(200,{'id':self.provider_id}))
        outcome=await push.send_message(session,'Alarm','Treść',self.delivery)
        self.assertEqual(outcome['status'],'delivered')
        args,options=session.calls[0]
        self.assertEqual(args,(push.ENDPOINT,))
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(options['json']['include_subscription_ids'],[self.recipient])
        self.assertNotIn('included_segments',options['json'])
        self.assertEqual(options['json']['idempotency_key'],self.delivery)
        self.assertEqual(options['headers']['Authorization'],'Key '+self.key.read_text())

    async def test_empty_id_is_not_a_success(self):
        outcome=await push.send_message(Session(Reply(200,{'id':'','errors':['internal']})),'Alarm','Treść',self.delivery)
        self.assertEqual(outcome['status'],'failed')
        self.assertEqual(outcome['code'],'onesignal_no_recipients')
        self.assertNotIn('internal',json.dumps(outcome))

    async def test_partial_recipient_failure_requires_a_decision(self):
        outcome=await push.send_message(Session(Reply(200,{'id':self.provider_id,'errors':{'invalid_player_ids':['private-id']}})),'Alarm','Treść',self.delivery)
        self.assertEqual(outcome['code'],'onesignal_partial')
        self.assertNotIn('private-id',json.dumps(outcome))

    async def test_http_failures_and_redirects_do_not_confirm_delivery(self):
        for status in (302,401,408,429,503):
            with self.subTest(status=status):
                outcome=await push.send_message(Session(Reply(status,{}, {'Retry-After':'120'})),'Alarm','Treść',self.delivery,now=1000)
                self.assertEqual(outcome['status'],'pending' if status in (408,429,503) else 'failed')
                self.assertEqual(outcome['retry_after'],1120)

    async def test_timeout_can_retry_with_the_same_delivery_id(self):
        outcome=await push.send_message(Session(TimeoutError()),'Alarm','Treść',self.delivery)
        self.assertEqual(outcome['status'],'pending')

    async def test_invalid_success_response_is_uncertain_and_can_retry(self):
        session=Session(Reply(200,{'id':'invalid'}))
        outcome=await push.send_message(session,'Alarm','Treść',self.delivery)
        self.assertEqual(outcome['status'],'pending')
        self.assertEqual(outcome['code'],'onesignal_response')

    async def test_missing_or_invalid_recipients_never_fall_back_to_all_users(self):
        for recipients in ([],['not-uuid'],{'included_segments':['All']}):
            self.recipients.write_text(json.dumps(recipients))
            session=Session(Reply(200,{'id':self.provider_id}))
            outcome=await push.send_message(session,'Alarm','Treść',self.delivery)
            self.assertEqual(outcome['status'],'failed')
            self.assertEqual(session.calls,[])

    async def test_invalid_secret_is_not_returned_or_sent(self):
        self.key.write_text('os_v2_app_'+'x'*40+'\nHeader: private')
        session=Session(Reply(200,{'id':self.provider_id}))
        outcome=await push.send_message(session,'Alarm','Treść',self.delivery)
        self.assertEqual(session.calls,[])
        self.assertNotIn('private',json.dumps(outcome))

    async def test_queue_payload_does_not_transmit_raw_data_or_coordinates(self):
        job=dict(destination_url=push.ENDPOINT,created_at=1000,job_id=self.delivery,
                 payload_json=json.dumps(dict(action='updated',configured_name='Nazwa źródłowa',
                     operator='PGE Dystrybucja',object_latitude=52.1,source_event={'secret':'PRIVATE'})))
        with patch('onesignal_push.send_message',return_value={'status':'delivered'}) as sender:
            self.assertEqual((await push.deliver(None,job,now=1001))['status'],'delivered')
        self.assertIn('Nazwa źródłowa',sender.call_args.args[2])
        self.assertNotIn('PRIVATE',str(sender.call_args))
        self.assertNotIn('52.1',str(sender.call_args))
        self.assertEqual(sender.call_args.args[3],self.delivery)

    async def test_expired_job_does_not_create_a_new_send_after_deduplication_window(self):
        job=dict(destination_url=push.ENDPOINT,created_at=0)
        with patch('onesignal_push.send_message') as sender:
            self.assertEqual((await push.deliver(None,job,now=30*86400))['code'],'onesignal_expired')
        sender.assert_not_called()

    async def test_wrong_destination_and_unknown_provider_fail_closed(self):
        session=Session(Reply(200,{}))
        self.assertEqual((await push.deliver(session,{'destination_url':'https://other.invalid'},now=1))['code'],'onesignal_destination')
        with patch.dict(os.environ,PGE_NOTIFICATION_PROVIDER='typo'):
            self.assertEqual((await queue.deliver(session,{}))['status'],'failed')
        self.assertEqual(session.calls,[])


class PushSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.ApiTests()
        await self.fixture.asyncSetUp()
        self.client=self.fixture.client
    async def asyncTearDown(self): await self.fixture.asyncTearDown()

    async def test_domain_change_does_not_initialize_old_site_subscription(self):
        with patch.dict(os.environ, PGE_ONESIGNAL_APP_ID=str(uuid.uuid4()),
                        PGE_ONESIGNAL_SITE_ORIGIN='https://old.example.org',
                        PGE_PUBLIC_ORIGIN='https://new.example.org'):
            response = await self.client.get('/api/admin/push/config')
            self.assertEqual(response.status, 503)
            payload = await response.json()
            self.assertNotIn('app_id', payload)
            self.assertIn('nowego adresu', payload['error'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with patch.dict(os.environ, PGE_ONESIGNAL_APP_ID=str(uuid.uuid4()),
                        PGE_ONESIGNAL_SITE_ORIGIN='https://new.example.org',
                        PGE_PUBLIC_ORIGIN='https://new.example.org'):
            self.assertEqual((await self.client.get('/api/admin/push/config')).status, 200)

    async def test_setup_and_configuration_require_admin_session_and_omit_secrets(self):
        with patch.dict(os.environ,PGE_ONESIGNAL_APP_ID=str(uuid.uuid4()),PGE_ONESIGNAL_API_KEY_FILE='/PRIVATE'):
            response=await self.client.get('/api/admin/push/config')
            self.assertEqual(response.status,200)
            self.assertEqual(set(await response.json()),{'app_id'})
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            page=await self.client.get('/api/admin/push/setup')
            self.assertEqual(page.status,200)
            self.assertIn('Alarmy push',await page.text())
            self.client.session.cookie_jar.clear()
            for path in ('/api/admin/push/setup','/api/admin/push/config'):
                self.assertEqual((await self.client.get(path)).status,401)

    async def test_unconfigured_state_and_worker_are_safe(self):
        with patch.dict(os.environ,PGE_ONESIGNAL_APP_ID=''):
            self.assertEqual((await self.client.get('/api/admin/push/config')).status,503)
        self.client.session.cookie_jar.clear()
        response=await self.client.get('/osd-push/OneSignalSDKWorker.js')
        self.assertEqual(response.status,200)
        self.assertIn('OneSignalSDK.sw.js',await response.text())
