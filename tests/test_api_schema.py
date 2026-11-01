"""Walidacja rzeczywistych odpowiedzi instancji testowej względem OpenAPI."""
import copy
import json
from pathlib import Path
import unittest
from urllib.parse import quote

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import test_api_v1

DOCUMENT = json.loads((Path(__file__).resolve().parents[1] / 'docs/openapi-v1.json').read_text(encoding='utf-8'))
REGISTRY = Registry().with_resource('urn:osd:openapi', Resource.from_contents(DOCUMENT, default_specification=DRAFT202012))


def validator(name):
    return Draft202012Validator({'$ref': 'urn:osd:openapi#/components/schemas/' + name},
                                registry=REGISTRY, format_checker=FormatChecker())


class ApiSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = test_api_v1.PublicApiV1Tests('test_index_summary_and_sources_are_public')
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_compact_full_raw_and_history_responses_match_the_contract(self):
        for route, name in [('/api/v1/events?view=compact', 'EventPage'),
                            ('/api/v1/events?view=full', 'EventPage'),
                            ('/api/v1/events?fields=start_at', 'EventPage'),
                            ('/api/v1/history?limit=2', 'HistoryPage')]:
            with self.subTest(route=route):
                response = await self.case.client.get(route)
                self.assertEqual(response.status, 200)
                validator(name).validate(await response.json())
        payload = await (await self.case.client.get('/api/v1/events?view=full')).json()
        identifier = quote(payload['items'][0]['event_id'], safe='')
        response = await self.case.client.get('/api/v1/events/' + identifier + '?include_raw=1')
        self.assertEqual(response.status, 200)
        validator('EventDetail').validate(await response.json())
        validator('EventComplete').validate(payload['items'][0])

    async def test_error_response_and_legacy_snapshot_are_valid(self):
        response = await self.case.client.get('/api/v1/events?limit=0')
        self.assertEqual(response.status, 400)
        validator('ApiError').validate(await response.json())
        history = await (await self.case.client.get('/api/v1/history?limit=2')).json()
        old = copy.deepcopy(history['items'][0])
        for snapshot in (old, old['after']):
            snapshot.pop('match_explanation', None)
        validator('HistoryItem').validate(old)

    async def test_missing_id_invalid_time_and_matching_explanation_are_rejected(self):
        payload = await (await self.case.client.get('/api/v1/events?view=full')).json()
        for mutate in ('missing_id', 'invalid_time', 'bad_explanation'):
            with self.subTest(mutate=mutate):
                bad = copy.deepcopy(payload)
                if mutate == 'missing_id':
                    del bad['items'][0]['event_id']
                elif mutate == 'invalid_time':
                    bad['items'][0]['start_at'] = 'tomorrow'
                else:
                    bad['items'][0]['match_explanation'] = {'version': 100}
                with self.assertRaises(ValidationError):
                    validator('EventPage').validate(bad)

    def test_schema_definitions_are_valid(self):
        for name, schema in DOCUMENT['components']['schemas'].items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
