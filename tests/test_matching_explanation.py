"""R8: prawdziwe ścieżki decyzji, historii, API i trwałej kolejki; bez OSD HTTP."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import patch

import app
import osd_sources
from geometry import normalize_geometry, geometry_contains, geometry_decision
from matching_evidence import matching_evidence, point_confirmed
from matching_explanation import explain, explanation_details, evidence_signature
from address_matching import address_binding
from event_history import project_event
import test_notification_queue
from test_notification_queue import sample_event
from test_matching_evidence import verified_audit, OUTER, HOLE


class ExplanationTests(unittest.TestCase):

    def test_geometry_holes_boundaries_and_multipolygon_union(self):
        g=normalize_geometry({'type':'Polygon','coordinates':[OUTER,HOLE]})
        for lat,lon,result,inside in [(50.25,20.25,'inside',True),(50,20,'outer_boundary',True),
                                     (51,21,'hole',False),(50.5,21,'hole_boundary',False),(54,24,'outside',False)]:
            d=geometry_decision(lat,lon,g);self.assertEqual((d['result'],d['contains']),(result,inside))
            self.assertEqual(geometry_contains(lat,lon,g),inside)
        union=normalize_geometry({'type':'MultiPolygon','coordinates':[[OUTER,HOLE],[OUTER]]})
        self.assertEqual(geometry_decision(51,21,union),{'contains':True,'result':'inside','polygon_index':1})

    def test_polygon_without_audit_and_with_audit_share_decision_not_confirmation(self):
        event={'source':'energa','geometry':normalize_geometry({'type':'Polygon','coordinates':[OUTER]}),'addresses':[]}
        location={'id':'one','city_sym':'1','latitude':51,'longitude':21}
        for audited in [False,True]:
            if audited:location['location_audit']=verified_audit(location)
            d=app.canonical_matching_decision(event,location,{},1000);e=matching_evidence(event,location,d['method'],d)
            x=explain(d,e,'test');self.assertEqual(e['point_confirmed'],audited)
            self.assertEqual(x['confidence'],'high' if audited else 'unknown')
            self.assertEqual(x['details']['geometry']['result'],'inside')

    def test_proximity_records_actual_minimum_limit_and_tauron_ignores_radius(self):
        event={'source':'tauron','center':{'latitude':51,'longitude':21},'radius_m':9000,
               'coordinates':[{'latitude':51.02,'longitude':21},{'latitude':51.001,'longitude':21}], 'addresses':[]}
        location={'city_sym':'1','latitude':51,'longitude':21}
        d=app.canonical_matching_decision(event,location,{},1000)
        self.assertEqual(d['method'],'spatial_nearby')
        p=d['details']['proximity'];self.assertEqual(p['latitude'],51.001)
        self.assertLess(p['distance_m'],120);self.assertEqual(p['limit_source'],'application_config')
        self.assertFalse(app.canonical_matching_decision(event,location,{},1)['matched'])
        event['source']='stoen';d=app.canonical_matching_decision(event,location,{},1)
        self.assertEqual(d['method'],'spatial_radius');self.assertEqual(d['details']['proximity']['limit_source'],'operator_radius')

    def test_exact_teryt_distinguishes_street_from_locality(self):
        raw=sample_event();event=osd_sources.normalize_pge(raw)
        for street,basis,level in [('', 'locality','low'),('11111','street','medium')]:
            location={'city_sym':'0123456','street_sym':street}
            d=app.canonical_matching_decision(event,location,{},1000)
            e=matching_evidence(event,location,d['method'],d);x=explain(d,e,'test')
            self.assertEqual((x['basis'],x['confidence']),(basis,level))
            self.assertFalse(e['point_confirmed'])

    def test_explanation_projection_legacy_unknown_and_cosmetic_signature(self):
        event={'match_confidence':'exact_teryt','match_evidence':{'version':2,'scope':'candidate','point_confirmed':False}}
        self.assertEqual(explanation_details(event)['reason_code'],'legacy_explanation_missing')
        self.assertFalse(point_confirmed(event))
        d={'method':'exact_teryt','details':{'address':{'result':'unknown','reason':'A'}},'limitations':[]}
        event['match_explanation']=explain(d,dict(scope='candidate',point_confirmed=False,reason='A'),'test')
        changed=copy.deepcopy(event);changed['match_explanation'].update(reason='B',version=99,rule_version='cosmetic')
        changed['match_explanation']['details']['address']['reason']='B'
        changed['match_evidence']['reason']='B'
        self.assertEqual(evidence_signature(event),evidence_signature(changed))
        self.assertNotIn('details',explanation_details(event,'compact'))
        self.assertIn('details',explanation_details(event,'full'))

    def test_address_geometry_conflict_is_explicit_and_cannot_confirm(self):
        event=osd_sources.normalize_pge(sample_event())
        event['geometry']=normalize_geometry({'type':'Polygon','coordinates':[OUTER]})
        location={'id':'one','city_sym':'0123456','street_sym':'11111','house_number':'1','latitude':54,'longitude':24}
        location['address_audit']=dict(status='verified',object_address_confirmed=True,street_context_confirmed=True,
            checked_at='2026-10-08T18:00:00Z',sources=[dict(reference='synthetic',sha256='a'*64)],binding=address_binding(location))
        d=app.canonical_matching_decision(event,location,{},1000)
        e=matching_evidence(event,location,d['method'],d);x=explain(d,e,'test')
        self.assertEqual(d['method'],'address_geometry_conflict')
        self.assertEqual((x['basis'],x['confidence']),('conflict','unknown'))
        self.assertFalse(e['point_confirmed']);self.assertEqual(x['details']['geometry']['result'],'outside')



class ScanExplanationTests(test_notification_queue.ScanQueueTests):

    async def test_evidence_only_change_is_archived_once_without_notification(self):
        original=app.matching_evidence
        def candidate(*args):
            e=original(*args);e.update(scope='candidate',point_confirmed=False,confirmation_basis=None);return e
        with patch('app.matching_evidence',side_effect=candidate):await self.scan(sample_event())
        before=self.store.read_history()['total'];self.assertEqual(self.jobs(),[])
        await self.scan(sample_event());await self.scan(sample_event())
        h=self.store.read_history();self.assertEqual(h['total'],before+1)
        self.assertEqual(h['items'][0]['action'],'updated');self.assertEqual(self.jobs(),[])
        self.assertFalse(h['items'][0]['before']['match_evidence']['point_confirmed'])
        self.assertTrue(h['items'][0]['after']['match_evidence']['point_confirmed'])

    async def test_enrichment_once_and_actual_change_not_hidden(self):
        await self.scan(sample_event());old_jobs=len(self.jobs())
        event=next(iter(self.application[app.STATE_KEY]['events'].values()));event.pop('match_explanation')
        count=self.store.read_history()['total']
        await self.scan(sample_event());await self.scan(sample_event())
        self.assertEqual(self.store.read_history()['total'],count+1)
        self.assertEqual(self.store.read_history()['items'][0]['origin'],'baseline')
        self.assertEqual(len(self.jobs()),old_jobs)
        next(iter(self.application[app.STATE_KEY]['events'].values())).pop('match_explanation')
        await self.scan(sample_event(description='Actual change'))
        self.assertEqual(self.store.read_history()['items'][0]['origin'],'scan')
        self.assertEqual(len(self.jobs()),old_jobs+1)

    async def test_cosmetic_reason_and_source_clock_do_not_append_history(self):
        await self.scan(sample_event());count=self.store.read_history()['total']
        original=app.matching_evidence
        def cosmetic(*args):
            e=original(*args);e['reason']='Inny polski tekst';return e
        with patch('app.matching_evidence',side_effect=cosmetic):await self.scan(sample_event())
        self.assertEqual(self.store.read_history()['total'],count)
        self.assertEqual(len(self.jobs()),1)

    async def test_conflict_and_unknown_address_do_not_queue(self):
        raw=sample_event();raw['addresses'].append({'teryt':{'citySym':'0123456','streetSymUl':'11111'},'numbers':'nieznane'})
        await self.scan(raw)
        e=next(iter(self.application[app.STATE_KEY]['events'].values()))
        self.assertFalse(point_confirmed(e));self.assertIn('numbers_unparsed',e['match_explanation']['limitations'])
        self.assertEqual(e['match_explanation']['details']['address']['unknown_row_count'],1)
        self.assertEqual(self.jobs(),[])

    async def test_api_full_compact_304_map_and_history(self):
        await self.scan(sample_event())
        client=self.case.client
        full=await (await client.get('/api/v1/events?view=full')).json()
        compact=await (await client.get('/api/v1/events')).json()
        self.assertIn('details',full['items'][0]['match_explanation'])
        self.assertNotIn('details',compact['items'][0]['match_explanation'])
        # Freshness contains a clock-dependent next_scan_at; compare at one instant.
        fixed = datetime.now(timezone.utc)
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz is not None else fixed.astimezone().replace(tzinfo=None)
        with patch('api_v1.datetime', FixedDatetime):
            response=await client.get('/api/v1/map');etag=response.headers['ETag']
            self.assertEqual((await client.get('/api/v1/map',headers={'If-None-Match':etag})).status,304)
            event=next(iter(self.application[app.STATE_KEY]['events'].values()));event['match_explanation']['reason']='Nowy opis'
            self.assertEqual((await client.get('/api/v1/map',headers={'If-None-Match':etag})).status,200)
        h=await (await client.get('/api/v1/history')).json()
        self.assertIn('match_explanation',h['items'][0]['after'])


if __name__=='__main__':unittest.main()
