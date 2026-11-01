"""Uszkodzony kontrakt operatora nie jest pustym, poprawnym odczytem."""

import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import osd_sources as sources
from test_app import sample_event


ENEA_INDEX = '<a href="index.php?rejon=1" title="1 wyłączenie">Miasto Testowe</a>'
ENEA_BLOCK = (
    '<div class="unpl block info"><h4 class="title_">Miasto Testowe</h4>'
    '<p class="description">ul. Testowa 1</p>'
    '<!-- 2026-10-07 08:00 - 2026-10-07 10:00 --></div>'
)
ENEA_TAIL = '<div class="unpl block info"><h4 class="title_">Drugie miasto</h4>'


class ParserContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_reproduction_enea_missing_counter(self):
        page = '<a href="index.php?rejon=1" title="Wyłączenia planowe">Rejon testowy</a>'
        with patch('osd_sources._request_text', AsyncMock(return_value=page)):
            with self.assertRaises(ValueError):
                await sources._fetch_enea_mode(None, sources.ENEA_BASE_URL, 'planned_outage', [])

    def test_reproduction_enea_truncated_second_block(self):
        with self.assertRaises(ValueError):
            sources.parse_enea_events(ENEA_BLOCK + ENEA_TAIL, 'planned_outage', 'https://example.test')

    async def test_reproduction_pge_text_revoked(self):
        raw = sample_event(revoked='true')
        with patch('osd_sources._request_json', AsyncMock(return_value=[raw])):
            with self.assertRaises(ValueError):
                await sources.fetch_pge(None, {})

    async def test_reproduction_pge_null_address(self):
        raw = dict(sample_event(), addresses=[None])
        with patch('osd_sources._request_json', AsyncMock(return_value=[raw])):
            with self.assertRaises(ValueError):
                await sources.fetch_pge(None, {})

    async def test_reproduction_tauron_text_active(self):
        with patch('osd_sources._request_json', AsyncMock(return_value=[{'OutageId': 'one', 'TypeId': 1, 'IsActive': 'false'}])):
            with self.assertRaises(ValueError):
                await sources.fetch_tauron(None, {})

    async def test_reproduction_stoen_conflict_on_short_last_page(self):
        pages = [[{'id': 'same', 'comment': 'first'}, {'id': 'other'}], [{'id': 'same', 'comment': 'changed'}]]
        with patch('osd_sources._request_json', AsyncMock(side_effect=pages)):
            with self.assertRaises(ValueError):
                await sources._fetch_stoen_planned(None, 'https://example.test', page_size=2)

    def test_boolean_contract_for_both_normalizers(self):
        for normalizer, raw, field, statuses in (
            (sources.normalize_pge, sample_event(), 'revoked', ('active', 'cancelled')),
            (sources.normalize_tauron, {'OutageId': 'one', 'TypeId': 1}, 'IsActive', ('resolved', 'active')),
        ):
            for value, status in zip((False, True), statuses):
                self.assertEqual(normalizer(dict(raw, **{field: value}))['status'], status)
            for bad in ('true', 'false', 0, 1, None, [], {}):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    normalizer(dict(raw, **{field: bad}))
            missing = deepcopy(raw)
            missing.pop(field, None)
            with self.subTest(field=field, missing=True), self.assertRaises(ValueError):
                normalizer(missing)

    def test_pge_preserves_legal_missing_address_information(self):
        for address in ({}, {'teryt': None}, {'teryt': {}}, {'teryt': {'citySym': '0123456', 'streetSymUl': None}}, {'teryt': {'citySym': '', 'streetSymUl': ''}}):
            for numbers in ('1, 3-7', ['1', '3A'], None):
                row = dict(address, numbers=numbers)
                event = sources.normalize_pge(dict(sample_event(), addresses=[row], coordinates=[], startAt=None, stopAt=None))
                self.assertEqual(event['addresses'], [row])
        self.assertEqual(sources.normalize_pge(dict(sample_event(), addresses=[]))['addresses'], [])

    def test_pge_rejects_corrupted_address_structure(self):
        addresses = [None, 'address', 2, []]
        addresses += [{'teryt': bad} for bad in ('text', 1, [], True)]
        addresses += [{'teryt': {key: bad}} for key in ('citySym', 'streetSymUl') for bad in (True, 123, [], {}, 'broken')]
        for address in addresses:
            with self.subTest(address=address), self.assertRaises(ValueError):
                sources.normalize_pge(dict(sample_event(), addresses=[address]))

    async def test_one_bad_record_rejects_whole_pge_and_tauron_list(self):
        for fetcher, good, bad in (
            (sources.fetch_pge, sample_event(), dict(sample_event(), uuid='two', addresses=[None])),
            (sources.fetch_tauron, {'OutageId': 'one', 'IsActive': True}, {'OutageId': 'two', 'IsActive': 'false'}),
        ):
            with patch('osd_sources._request_json', AsyncMock(return_value=[good, bad])):
                with self.assertRaises(ValueError):
                    await fetcher(None, {})

    def test_enea_rejects_truncated_links_and_ambiguous_activity(self):
        for page in (
            '<a href="index.php?rejon=1" title="1 awaria">urwany',
            ENEA_INDEX + '<a href="index.php?rejon=2"',
            '<a href="index.php?rejon=1">Miasto 123</a>',
            ENEA_INDEX + ' Brak wyłączeń',
            '<a href="index.php?rejon=1">region</a> Brak wyłączeń',
            '<a href="index.php?rejon=1" title="0 wyłączeń, 1 awaria">region</a>',
            ENEA_INDEX + '<a href="index.php?rejon=1" title="0 wyłączeń">region</a>',
        ):
            with self.subTest(page=page), self.assertRaises(ValueError):
                sources.enea_active_regions(page)

    def test_enea_accepts_explicit_empty_and_filtered_regions(self):
        for page in ('Brak wyłączeń', 'Brak awarii', '<a href="index.php?rejon=1" title="0 wyłączeń">region</a>', '<a href="index.php?rejon=1" title="brak planowanych wyłączeń">region</a>'):
            self.assertEqual(sources.enea_active_regions(page), [])
        page = ENEA_INDEX + '<a href="index.php?rejon=2">nieznany stan</a>'
        self.assertEqual(sources.enea_active_regions(page, [1]), [1])
        self.assertEqual(sources.enea_active_regions('<a href="index.php?rejon=2" title="2 awarie">region</a>'), [2])

    def test_enea_unrelated_empty_phrase_is_not_a_valid_empty_page(self):
        for page in ('Nie odnotowano zmian strony', '<script>Brak wyłączeń</script>'):
            self.assertFalse(sources.enea_index_is_recognized(page))
            self.assertFalse(sources.enea_region_page_is_recognized(page))

    def test_enea_rejects_incomplete_and_nested_blocks(self):
        for page in (ENEA_TAIL, ENEA_TAIL + ' Brak wyłączeń', ENEA_TAIL + ENEA_BLOCK + '</div>'):
            with self.subTest(page=page), self.assertRaises(ValueError):
                sources.parse_enea_events(page, 'outage', 'https://example.test')
        self.assertEqual(len(sources.parse_enea_events(ENEA_BLOCK, 'planned_outage', 'https://example.test')), 1)

    async def test_enea_index_count_is_not_atomic_with_details(self):
        with patch('osd_sources._request_text', AsyncMock(side_effect=[ENEA_INDEX, ENEA_BLOCK + ENEA_BLOCK])):
            self.assertEqual(len(await sources._fetch_enea_mode(None, sources.ENEA_BASE_URL, 'planned_outage', [])), 2)

    async def test_stoen_conflict_same_page_full_page_and_raw_field(self):
        for pages in (
            [[{'id': 'one', 'extra': 1}, {'id': 'one', 'extra': 2}]],
            [[{'id': 'one'}, {'id': 'two'}], [{'id': 'one', 'extra': 1}, {'id': 'three'}]],
        ):
            with self.subTest(pages=pages), patch('osd_sources._request_json', AsyncMock(side_effect=pages)):
                with self.assertRaises(ValueError):
                    await sources._fetch_stoen_planned(None, 'https://example.test', page_size=2)

    async def test_stoen_identical_duplicates_and_key_order(self):
        pages = [[{'id': 'one', 'comment': 'same'}, {'comment': 'same', 'id': 'one'}], []]
        with patch('osd_sources._request_json', AsyncMock(side_effect=pages)):
            rows = await sources._fetch_stoen_planned(None, 'https://example.test', page_size=2)
        self.assertEqual(rows, [{'id': 'one', 'comment': 'same'}])

    async def test_stoen_no_progress_and_page_limit_remain_errors(self):
        for pages, max_pages in (([[{'id': 'one'}], [{'id': 'one'}]], 100), ([[{'id': 'one'}]], 1)):
            with patch('osd_sources._request_json', AsyncMock(side_effect=pages)):
                with self.assertRaises(ValueError):
                    await sources._fetch_stoen_planned(None, 'https://example.test', page_size=1, max_pages=max_pages)
