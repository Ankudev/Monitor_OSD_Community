import unittest
from unittest.mock import patch

import app
from geometry import normalize_geometry
from matching_evidence import matching_evidence, match_details, point_confirmed, source_map_geometry, location_verification, EVIDENCE_VERSION
from address_matching import address_binding


OUTER = [[20,50],[22,50],[22,52],[20,52],[20,50]]
HOLE = [[20.5,50.5],[21.5,50.5],[21.5,51.5],[20.5,51.5],[20.5,50.5]]
def verified_audit(location):
    return {'status':'verified','object_point_confirmed':True,'binding':address_binding(location),
            'checked_at':'2026-10-06T00:00:00Z','sources':[{'reference':'https://example.test/object','sha256':'a'*64}]}


class MatchingEvidenceTests(unittest.TestCase):
    def test_locality_street_nearby_and_radius_are_not_point_confirmation(self):
        location = {'latitude':51,'longitude':21,'location_audit':{'method':'gps_city'}}
        for confidence in ['exact_teryt','text_address','text_keyword','spatial_nearby','spatial_radius','county_mass']:
            with self.subTest(confidence=confidence):
                evidence=matching_evidence({},location,confidence)
                self.assertEqual(evidence['scope'],'candidate')
                self.assertFalse(evidence['point_confirmed'])

    def test_verified_point_in_polygon_is_confirmed_but_hole_is_not(self):
        event={'geometry':normalize_geometry({'type':'Polygon','coordinates':[OUTER,HOLE]})}
        location={'latitude':50.25,'longitude':20.25}
        location['location_audit']=verified_audit(location)
        evidence=matching_evidence(event,location,'spatial_polygon')
        self.assertTrue(point_confirmed({'match_evidence':evidence}))
        location.update(latitude=51,longitude=21)
        self.assertFalse(matching_evidence(event,location,'spatial_polygon')['point_confirmed'])

    def test_audit_requires_current_binding_and_explicit_point_evidence(self):
        from copy import deepcopy
        location={'id':'one','city_sym':'0000001','street_sym':'','house_number':None,'latitude':51,'longitude':21}
        location['location_audit']=verified_audit(location)
        self.assertEqual(location_verification(location),'recorded_audit')
        for field,value in [('id','two'),('city_sym','0000002'),('street_sym','00123'),('house_number','3'),('latitude',52),('longitude',22)]:
            changed=deepcopy(location);changed[field]=value
            self.assertNotEqual(location_verification(changed),'recorded_audit')
        for field,value in [('status','unknown'),('object_point_confirmed',False),('sources',[]),('sources',[{'reference':'x','sha256':'bad'}]),('checked_at','bad'),('requires_review',True)]:
            changed=deepcopy(location);changed['location_audit'][field]=value
            self.assertNotEqual(location_verification(changed),'recorded_audit')
        location['location_audit']={'method':'gps_city','checked_at':'2026-10-02T00:00:00Z','source_url':'https://example.test/ref'}
        self.assertEqual(location_verification(location),'not_audited')

    def test_old_positive_evidence_does_not_confirm_after_release(self):
        self.assertFalse(point_confirmed({'match_evidence':{'version':1,'scope':'confirmed_point','point_confirmed':True}}))
        self.assertTrue(point_confirmed({'match_evidence':{'version':EVIDENCE_VERSION,'scope':'confirmed_point','point_confirmed':True}}))

    def test_location_evidence_is_private_in_object_api_projection(self):
        location={'id':'one','name':'Obiekt','city_sym':'0000001','latitude':51,'longitude':21}
        location['location_audit']=verified_audit(location)
        location['location_audit']['sources'][0]['file']='C:/private-evidence.txt'
        public=app.enriched_objects([location],app.Path('missing-teryt.db'))[0]
        self.assertEqual(public['location_audit']['status'],'verified')
        self.assertNotIn('private-evidence',str(public))
        self.assertNotIn('sources',public['location_audit'])

    def test_audit_only_change_invalidates_matching_fingerprint(self):
        from copy import deepcopy
        config={'teryt_locations':[{'id':'one','latitude':51,'longitude':21}]}
        changed=deepcopy(config)
        changed['teryt_locations'][0]['location_audit']=verified_audit(changed['teryt_locations'][0])
        self.assertNotEqual(app.object_fingerprints(config),app.object_fingerprints(changed))

    def test_missing_or_invalidated_audit_and_missing_geometry_fail_closed(self):
        event={'geometry':normalize_geometry({'type':'Polygon','coordinates':[OUTER]})}
        for metadata in [{},{'requires_review':True,'review_reason':'location_changed'}, {'method':'gps_city'}]:
            self.assertFalse(matching_evidence(event,{'latitude':51,'longitude':21,'location_audit':metadata},'spatial_polygon')['point_confirmed'])
        self.assertFalse(matching_evidence({}, {'location_audit':{'method':'gps_city'}}, 'spatial_polygon')['point_confirmed'])

    def test_legacy_assignment_and_missing_source_read_do_not_confirm_point(self):
        self.assertEqual(match_details({'match_confidence':'exact_teryt'})['scope'],'unknown')
        evidence={'version':1,'scope':'confirmed_point','point_confirmed':True}
        self.assertFalse(point_confirmed({'match_evidence':evidence,'verification_pending':True}))

    def test_notification_schedule_skips_candidates_and_legacy_records(self):
        runtime={app.RUNTIME_KEY:{'last_notifications':['previous']}}
        with patch('app.asyncio.create_task') as create:
            self.assertIsNone(app.schedule_notifications(runtime,'https://example.test',[('new',{'match_confidence':'spatial_radius'})]))
        create.assert_not_called()
        # Helper wybudzający nie zmienia trwałych wyników poprzednich zadań.
        self.assertEqual(runtime[app.RUNTIME_KEY]['last_notifications'],['previous'])

    def test_malformed_candidate_evidence_cannot_confirm_point(self):
        for evidence in [{'version':1,'scope':'candidate','point_confirmed':True},
                         {'version':1,'scope':'candidate'}, ['invalid']]:
            with self.subTest(evidence=evidence):
                self.assertFalse(point_confirmed({'match_evidence':evidence}))

    def test_independent_points_remain_multipoint_and_invalid_points_are_skipped(self):
        event={'coordinates':[{'latitude':52,'longitude':20},{'latitude':53,'longitude':21},{'latitude':54,'longitude':22},{'latitude':None,'longitude':None}]}
        geometry=source_map_geometry(event)
        self.assertEqual(geometry,{'type':'MultiPoint','coordinates':[[20.0,52.0],[21.0,53.0],[22.0,54.0]]})


if __name__ == '__main__': unittest.main()
