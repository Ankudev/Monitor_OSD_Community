import unittest
from copy import deepcopy

import app
from api_v1 import _object
from power_supply import normalize_power_supply, merge_power_supply
from test_app import sample_config


class PowerSupplyTests(unittest.TestCase):
    def test_partial_updates_preserve_independent_equipment(self):
        current = {'generator': 'szr', 'secondary_feed': 'manual', 'notes': 'Sprawdzić paliwo'}
        self.assertEqual(merge_power_supply(current, {}), current)
        result = merge_power_supply(current, {'generator': 'none'})
        self.assertEqual(result, dict(current, generator='none'))
        self.assertEqual(current['generator'], 'szr')

    def test_defaults_and_validation_do_not_invent_equipment(self):
        self.assertEqual(normalize_power_supply({}), {'generator':'unknown','secondary_feed':'unknown','notes':''})
        for value in ('unknown', 'none', 'manual', 'szr'):
            self.assertEqual(normalize_power_supply({'generator':value})['generator'], value)
        for bad in (None, [], 'text', {'generator':True}, {'secondary_feed':0}, {'generator':'present'}, {'extra':'x'}, {'notes':None}, {'notes':'x'*1001}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_power_supply(bad)

    def test_power_edit_keeps_all_operator_fingerprints_but_gps_edit_does_not(self):
        config = sample_config()
        before = {s:app.object_fingerprints(config,s) for s in app.DEFAULT_SOURCES}
        config['teryt_locations'][0]['power_supply'] = {'generator':'szr','notes':'Paliwo'}
        for source, value in before.items():
            self.assertEqual(app.object_fingerprints(config,source), value)
        config['teryt_locations'][0]['latitude'] += 0.1
        for source, value in before.items():
            self.assertNotEqual(app.object_fingerprints(config,source), value)

    def test_normalization_restart_and_public_views_keep_profile_and_audits(self):
        original = deepcopy(sample_config()['teryt_locations'][0])
        old = app.normalize_locations([original])[0]
        self.assertNotIn('power_supply', old)  # bez masowego dopisywania do starego pliku
        self.assertEqual(_object(old)['power_supply']['generator'], 'unknown')
        original.update(power_supply={'generator':'manual','secondary_feed':'szr','notes':' <b>Paliwo</b> '}, location_audit={'requires_review':True})
        normalized = app.normalize_locations([original])[0]
        again = app.normalize_locations([normalized])[0]
        self.assertEqual(normalized, again)
        self.assertEqual(again['id'], original['id'])
        self.assertEqual(again['location_audit'], original['location_audit'])
        self.assertNotIn('notes', _object(again)['power_supply'])
        self.assertEqual(_object(again,'full')['power_supply']['notes'], '<b>Paliwo</b>')
