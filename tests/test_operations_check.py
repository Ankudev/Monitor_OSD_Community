"""Ocena alarmĂłw i rzeczywisty lokalny odczyt prywatnego API bez wysyĹ‚ania wiadomoĹ›ci."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
import test_operations as fixtures
from operations_check import evaluate, transitions
from runtime_health import OperationsToken
from tools.check_operations import check, fetch_operations
from tools.check_operations import main as monitor_main


def healthy():
    return dict(local_ready=True, jobs={'pge':{'state':'ok','phase':'waiting'},
                'notifications':{'state':'disabled'}}, sources={'pge':{'stale':False}},
                disk={'free_bytes':10**10,'total_bytes':10**11,'inodes':None},
                api={'sample_count':0,'errors_5xx':0,'p95_seconds':None},
                backups={'state':'fresh','offsite_verified':False},
                notifications={'enabled':False})


class EvaluationTests(unittest.TestCase):
    def test_waiting_and_disabled_jobs_are_healthy(self):
        self.assertEqual(evaluate(healthy()),[])

    def test_stale_source_is_separate_from_stopped_task_and_storage_failure(self):
        data = healthy()
        data['sources']['pge']['stale'] = True
        self.assertEqual({row['code']:row['severity'] for row in evaluate(data)}, {'source:pge':'warning'})
        data['jobs']['pge']['state'] = 'stalled'
        data['local_ready'] = False
        result = {row['code']:row['severity'] for row in evaluate(data)}
        self.assertEqual(result,{'source:pge':'warning','job:pge':'critical','local_storage':'critical'})

    def test_disk_bytes_ratio_and_inodes_are_checked(self):
        for free,total,inodes,expected in [(100,10**10,None,'disk_space'),
                (10**9,10**11,None,'disk_space'),(10**10,10**11,{'free':1,'total':100},'disk_inodes')]:
            data = healthy()
            data['disk'].update(free_bytes=free,total_bytes=total,inodes=inodes)
            self.assertIn(expected,{row['code'] for row in evaluate(data)})

    def test_latency_and_errors_need_enough_samples(self):
        data = healthy()
        data['api'].update(sample_count=19,errors_5xx=19,p95_seconds=10)
        self.assertEqual(evaluate(data),[])
        data['api']['sample_count'] = 20
        self.assertEqual({row['code'] for row in evaluate(data)},{'api_latency','api_errors'})

    def test_future_notification_retry_is_not_overdue(self):
        data = healthy()
        data['notifications'] = dict(enabled=True,counts={'failed':0},blocked_by_failure=0,next_attempt_at=7200)
        self.assertEqual(evaluate(data,now=1000),[])
        data['notifications']['next_attempt_at'] = 0
        self.assertIn('notification_overdue',{row['code'] for row in evaluate(data,now=1000)})
        data['notifications']['counts']['failed'] = 1
        self.assertIn('notification_failed',{row['code'] for row in evaluate(data,now=1000)})

    def test_backup_failure_and_cleanup_have_different_levels(self):
        data = healthy()
        data['backups'].update(state='failed')
        self.assertEqual(evaluate(data)[0]['severity'],'critical')
        data['backups'].update(state='warning',retention_state='failed')
        self.assertEqual({row['code']:row['severity'] for row in evaluate(data)},
                         {'backup':'warning','backup_retention':'warning'})

    def test_malformed_or_missing_measurements_never_look_healthy(self):
        cases = [None, {}, dict(healthy(),jobs={}), dict(healthy(),sources={})]
        for modify in (lambda d:d['disk'].update(free_bytes=float('nan')),
                       lambda d:d['disk'].update(free_bytes=10**500),
                       lambda d:d['api'].update(sample_count=20,p95_seconds=None),
                       lambda d:d['backups'].update(state='unknown'),
                       lambda d:d['jobs']['pge'].update(state='missing')):
            data = healthy()
            modify(data)
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError): evaluate(data)

    def test_transitions_repeat_silently_and_outage_does_not_resolve_unknown_conditions(self):
        row = dict(code='source:pge',severity='warning',message='Test')
        active, changes = transitions({},[row])
        self.assertEqual(changes[0]['event'],'opened')
        self.assertEqual(transitions(active,[row])[1],[])
        missing = dict(code='probe_unavailable',severity='critical',message='Test')
        outage, changes = transitions(active,[missing],complete=False)
        self.assertIn('source:pge',outage)
        self.assertEqual([item['event'] for item in changes],['opened'])
        recovered, changes = transitions(outage,[])
        self.assertEqual(recovered,{})
        self.assertEqual([item['event'] for item in changes],['resolved','resolved'])

    def test_state_is_bound_to_origin_and_corruption_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'monitor.json'
            with patch('tools.check_operations.fetch_operations',return_value=healthy()):
                first = check('http://127.0.0.1:12345',path)
                self.assertEqual(first['state'],'ok')
                self.assertEqual(check('http://127.0.0.1:12345',path)['changes'],[])
                saved = path.read_bytes()
                with self.assertRaises(ValueError): check('http://127.0.0.1:12346',path)
                self.assertEqual(saved,path.read_bytes())
                path.write_text('{"version":1,"active":[]}',encoding='utf8')
                saved = path.read_bytes()
                with self.assertRaises(ValueError): check('http://127.0.0.1:12345',path)
                self.assertEqual(saved,path.read_bytes())

    def test_failed_probe_keeps_existing_alarm_and_emits_recovery_after_valid_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'monitor.json'
            data = healthy()
            data['sources']['pge']['stale'] = True
            with patch('tools.check_operations.fetch_operations',return_value=data):
                check('http://127.0.0.1:12345',path)
            with patch('tools.check_operations.fetch_operations',side_effect=OSError('SECRET')):
                failed = check('http://127.0.0.1:12345',path)
            self.assertFalse(failed['complete'])
            self.assertIn('source:pge',failed['active'])
            self.assertNotIn('SECRET',json.dumps(failed))
            self.assertEqual([row['event'] for row in failed['changes']],['opened'])
            with patch('tools.check_operations.fetch_operations',return_value=healthy()):
                recovered = check('http://127.0.0.1:12345',path)
            self.assertTrue(recovered['complete'])
            self.assertEqual(recovered['state'],'ok')
            self.assertEqual({row['code'] for row in recovered['changes']},{'source:pge','probe_unavailable'})

    def test_cli_reports_exit_codes_without_sending_any_notification(self):
        import io
        args = ['check_operations.py','--origin','http://127.0.0.1:12345','--state','unused.json']
        for state, expected in (('ok',0),('warning',1),('critical',2)):
            with patch('sys.argv',args), patch('tools.check_operations.check',return_value={'state':state,'notifications_sent':False}), patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(monitor_main(),expected)
                self.assertFalse(json.loads(output.getvalue())['notifications_sent'])
        with patch('sys.argv',args), patch('tools.check_operations.check',side_effect=OSError('SECRET')), patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(monitor_main(),3)
            self.assertNotIn('SECRET',output.getvalue())


class MonitorHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_reads_real_api_using_read_only_token(self):
        case = fixtures.OperationsApiTests('test_health_is_separate_from_upstream_failure_and_admin_is_protected')
        await case.asyncSetUp()
        try:
            root = Path(case.fixture.temp_directory.name)
            token = root/'monitor-token'
            token.write_text('x'*48)
            with patch.dict(os.environ,PGE_OPERATIONS_TOKEN_FILE=str(token)):
                case.application[app.OPERATIONS_TOKEN_KEY] = OperationsToken()
                origin = str(case.client.make_url('')).rstrip('/')
                payload = await asyncio.to_thread(fetch_operations,origin)
                self.assertTrue(payload['local_ready'])
                result = await asyncio.to_thread(check,origin,root/'monitor.json')
            self.assertTrue(result['complete'])
            self.assertFalse(result['notifications_sent'])
            self.assertNotIn('xxxx',json.dumps(result))
            self.assertFalse(case.application[app.START_SCANNER_KEY])
        finally:
            await case.asyncTearDown()
