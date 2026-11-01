"""Strona serwisowa nie usuwa komunikatów i nie udaje pustego indeksu."""
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import osd_sources as sources
from scan_diagnostics import SourceMaintenanceError, error_details

PAGE = '<html><body><h1>Prowadzone są prace administracyjne.</h1></body></html>'
INDEX = '<a href="index.php?rejon=1" title="1 wyłączenie">Rejon testowy</a>'


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_page_stops_both_modes_after_one_request(self):
        for kind in ('planned_outage', 'outage'):
            with self.subTest(kind=kind), patch('osd_sources._request_text', AsyncMock(return_value=PAGE)) as fetch:
                with self.assertRaises(SourceMaintenanceError) as caught:
                    await sources._fetch_enea_mode(None, sources.ENEA_BASE_URL, kind, [])
                self.assertEqual(fetch.await_count, 1)
                self.assertIn('prowadzone są prace administracyjne', str(caught.exception).lower())
                self.assertEqual(error_details(caught.exception)['error_code'], 'maintenance')

    async def test_region_maintenance_rejects_entire_result(self):
        with patch('osd_sources._request_text', AsyncMock(side_effect=[INDEX, PAGE])):
            with self.assertRaises(SourceMaintenanceError):
                await sources._fetch_enea_mode(None, sources.ENEA_BASE_URL, 'planned_outage', [])

    async def test_maintenance_overrides_unrelated_empty_phrase(self):
        with patch('osd_sources._request_text', AsyncMock(return_value=PAGE+'<p>Brak wyłączeń</p>')):
            with self.assertRaises(SourceMaintenanceError):
                await sources._fetch_enea_mode(None, sources.ENEA_BASE_URL, 'planned_outage', [])

    def test_normal_data_and_hidden_text_are_not_maintenance(self):
        for page in (
            INDEX + '<p>Prowadzone są prace administracyjne.</p>',
            '<div class="unpl block info"><p class="description">Prowadzone są prace administracyjne.</p></div>',
            '<html><script>Prowadzone są prace administracyjne.</script>Brak wyłączeń</html>',
            '<html><!-- Prowadzone są prace administracyjne. -->Brak wyłączeń</html>',
            '<p>Strona zablokowana</p>', '<p>Prace serwisowe stacji</p>', '',
        ):
            with self.subTest(page=page):
                sources._check_enea_maintenance(page, 'wyłączeń')

    async def test_unknown_page_remains_contract_error(self):
        with patch('osd_sources._request_text', AsyncMock(return_value='<p>Strona zablokowana</p>')):
            with self.assertRaises(sources.SourceContractError) as caught:
                await sources._fetch_enea_mode(None, sources.ENEA_BASE_URL, 'planned_outage', [])
            self.assertNotIsInstance(caught.exception, SourceMaintenanceError)
