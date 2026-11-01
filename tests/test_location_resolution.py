import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from contextlib import closing

import app


class LocationResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'teryt.db'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE simc(sym TEXT,nazwa TEXT,woj TEXT,pow TEXT,gmi TEXT);'
                             'CREATE TABLE ulic(sym TEXT,sym_ul TEXT,cecha TEXT,nazwa_1 TEXT,nazwa_2 TEXT);')
            db.execute('INSERT INTO simc VALUES(?,?,?,?,?)', ('0000001','Miasto','14','01','01'))
            db.executemany('INSERT INTO ulic VALUES(?,?,?,?,?)', [
                ('0000001','00001','ul.','Testowa',''), ('0000001','00002','al.','Testowa',''),
                ('0000001','00003','ul.','Testowa Druga','')])

    def test_ambiguous_street_is_not_first_hit_and_prefix_disambiguates(self):
        for road in ('Testowa','Test','ul. Test'):
            with self.subTest(road=road), self.assertRaises(app.TerytResolutionError) as ctx:
                app.teryt_from_address(self.path, {'city':'Miasto','road':road})
            self.assertEqual(ctx.exception.code,'ambiguous_street')
        for road,code in [('ul. Testowa','00001'),('aleja Testowa','00002'),('Testowa Druga','00003')]:
            self.assertEqual(app.teryt_from_address(self.path,{'city':'Miasto','road':road})['street_sym'],code)
        self.assertEqual(app.teryt_from_address(self.path,{'city':'Miasto','road':'%'} )['street_sym'],'')

    async def test_osm_preview_survives_teryt_failure_without_second_request(self):
        geocoder=AsyncMock()
        geocoder.request_json.return_value={'address':{'city':'Miasto','road':'Testowa'},'display_name':'Pomocniczy adres OSM'}
        with self.assertRaises(app.TerytResolutionError) as ctx:
            await app.resolved_location(geocoder,self.path,52,21)
        response=app.geocoder_error_response(ctx.exception)
        payload=json.loads(response.text)
        self.assertEqual(response.status,400)
        self.assertEqual(payload['code'],'ambiguous_street')
        self.assertEqual(payload['resolution_status'],'requires_review')
        self.assertEqual(payload['osm_result']['latitude'],52)
        self.assertEqual(payload['osm_result']['display_name'],'Pomocniczy adres OSM')
        self.assertNotIn('city_sym',payload['osm_result'])
        self.assertEqual(geocoder.request_json.await_count,1)

    async def test_ambiguous_city_preview_and_normal_success(self):
        geocoder=AsyncMock()
        geocoder.request_json.return_value={'address':{'city':'Miasto','road':'aleja Testowa'},'display_name':'Adres'}
        result=await app.resolved_location(geocoder,self.path,52,21)
        self.assertEqual(result['street_sym'],'00002')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO simc VALUES(?,?,?,?,?)',('0000002','Miasto','14','02','01'))
        with self.assertRaises(app.TerytResolutionError) as ctx:
            await app.resolved_location(geocoder,self.path,52,21)
        self.assertEqual(ctx.exception.code,'ambiguous_city')
        self.assertEqual(ctx.exception.osm_result['display_name'],'Adres')
