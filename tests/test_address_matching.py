import unittest
import hashlib
import tempfile
from pathlib import Path
from copy import deepcopy

import app
import test_app as base
from address_matching import (house_number, address_binding, address_verified,
                              address_decision, compare_numbers, public_address_audit)
from matching_evidence import matching_evidence
from osd_sources import normalize_tauron
from tools.verify_address import verify


def verified_location(number="12"):
    location = dict(id="site", city_sym="0123456", street_sym="12345", house_number=number,
                    latitude=51, longitude=21)
    location["address_audit"] = dict(status="verified", object_address_confirmed=True,
        street_context_confirmed=True, checked_at="2026-10-05T00:00:00Z",
        sources=[dict(reference="private-proof", sha256="a" * 64)], binding=address_binding(location))
    return location


def pge(numbers="12", street="12345"):
    rows = [dict(teryt=dict(citySym="0123456", streetSymUl=street), numbers=numbers)]
    return dict(source="pge", source_event_id="one", addresses=rows, raw=dict(addresses=rows))


class AddressMatchingTests(unittest.TestCase):
    def test_number_validation_and_identity(self):
        for value in [True, 12, [], {}, "1 2", "12-20", "dz. 123/4", "a" * 33]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                house_number(value)
        self.assertEqual(house_number(" 12a "), "12A")
        self.assertEqual(house_number(None), None)
        self.assertEqual(house_number("12/3"), "12/3")

    def test_positive_negative_and_unknown_matrix(self):
        cases = [("110", "110", "matches"), (["110"], "110", "matches"),
            ("11", "1", "does_not_match"), ("12A", "12", "does_not_match"),
            ("12", "12A", "does_not_match"), ("12/3", "12/3", "matches"),
            ("12-20", "12", "matches"), ("12-20", "20", "matches"),
            ("12-20", "12A", "does_not_match"), ("1, 3; 5", "3", "matches"),
            ("1-20 nieparzyste", "12", "does_not_match"),
            ("parzyste 2-20 oprócz 12", "12", "does_not_match"),
            ("2-20 parzyste", "14", "matches"), ("2-20 parzyste", "12A", "unknown"),
            ("od 9 do 13 nieparzyste", "11", "matches"),
            ("nr 18/20;", "18/20", "matches"), ("12A-12D", "12B", "unknown"),
            ("20-12", "14", "unknown"), ("dz. 911/3", "911/3", "unknown"),
            ("12, obok 14", "12", "unknown"), ("12 oprócz okolice 12", "12", "unknown"),
            ("", "12", "unknown"), ([], "12", "unknown"),
            ("1-999999999", "999999999", "matches")]
        for value, number, expected in cases:
            with self.subTest(value=value, number=number):
                self.assertEqual(compare_numbers(value, number)[0], expected)

    def test_audit_bound_to_exact_record_and_private_source_hidden(self):
        obj = verified_location()
        self.assertTrue(address_verified(obj))
        self.assertNotIn("private-proof", str(public_address_audit(obj)))
        for field, value in [("house_number", "14"), ("latitude", 52), ("city_sym", "0000001")]:
            changed = deepcopy(obj); changed[field] = value
            self.assertFalse(address_verified(changed))
        obj["location_audit"] = {"requires_review": True}
        self.assertFalse(address_verified(obj))

    def test_all_rows_and_incomplete_rows(self):
        obj = verified_location()
        event = pge("14")
        self.assertEqual(address_decision(event, obj)["result"], "does_not_match")
        event["raw"]["addresses"].append(pge("12")["addresses"][0])
        self.assertEqual(address_decision(event, obj)["result"], "matches")
        event = pge("14"); event["raw"]["addresses"].append({"text": "okolica"})
        self.assertEqual(address_decision(event, obj)["result"], "unknown")
        event = pge("12"); event["raw"]["addresses"].append({"text": "oprócz 12"})
        self.assertEqual(address_decision(event, obj)["result"], "unknown")
        for source in ["tauron", "enea", "energa", "stoen"]:
            event = pge(); event["source"] = source
            self.assertEqual(address_decision(event, obj)["result"], "unknown")

    def test_exact_address_geometry_conflicts_and_holes(self):
        obj = verified_location(); event = pge()
        self.assertEqual(app.canonical_matching_addresses(event, obj, {}, 1000)[1], "exact_address")
        self.assertTrue(matching_evidence(event, obj, "exact_address")["point_confirmed"])
        geometry = {"type": "Polygon", "coordinates": [[[20,50],[22,50],[22,52],[20,52],[20,50]]]}
        event = pge("14"); event["geometry"] = geometry
        self.assertEqual(app.canonical_matching_addresses(event, obj, {}, 1000)[1], "address_geometry_conflict")
        self.assertFalse(matching_evidence(event, obj, "address_geometry_conflict")["point_confirmed"])
        obj.pop("address_audit"); obj["latitude"] = 53
        self.assertFalse(app.canonical_matching_addresses(event, obj, {}, 1000)[0])

    def test_normalization_preserves_optional_evidence_and_absent_field(self):
        obj = verified_location()
        self.assertEqual(app.normalize_locations([obj])[0], {**obj, "name": "", "keyword": ""})
        obj.pop("house_number"); obj.pop("address_audit")
        self.assertNotIn("house_number", app.normalize_locations([obj])[0])

    def test_tauron_raw_radius_cannot_assign_distant_object(self):
        event = normalize_tauron({"OutageId": "one", "TypeId": 2, "IsActive": True, "Radius": 60000,
            "Center": {"lat": 51, "lng": 21}, "Coordinates": [{"lat": 51, "lng": 21}]})
        self.assertEqual(event["raw"]["Radius"], 60000)
        self.assertIsNone(event["radius_m"])
        obj = verified_location(); obj["latitude"] = 51.2
        self.assertFalse(app.canonical_matching_addresses(event, obj, {}, 1000)[0])

    def test_public_projection_cannot_leak_evidence(self):
        obj = verified_location()
        public = app.enriched_objects([obj], app.Path("missing-teryt.db"))[0]
        self.assertNotIn("private-proof", str(public))
        self.assertEqual(public_address_audit(public)["status"], "verified")

    def test_verification_tool_requires_actual_file_and_current_binding(self):
        obj = verified_location(); proof = deepcopy(obj.pop("address_audit"))
        proof["object_id"] = obj["id"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proof.txt"; path.write_bytes(b"synthetic address evidence")
            proof["sources"] = [{"file": str(path), "reference": "Synthetic test only",
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
            self.assertTrue(address_verified(verify([deepcopy(obj)], proof)[0]))
            proof["binding"]["house_number"] = "99"
            with self.assertRaises(ValueError):
                verify([deepcopy(obj)], proof)
            proof["binding"] = address_binding(obj); proof["sources"][0]["sha256"] = "b" * 64
            with self.assertRaises(ValueError):
                verify([deepcopy(obj)], proof)


class AddressApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = base.ApiTests("test_status_endpoint")
        await self.case.asyncSetUp()
        self.client = self.case.client

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_patch_preserve_clear_invalidate_and_persist(self):
        obj = self.case.app[app.CONFIG_KEY]["teryt_locations"][0]
        obj["house_number"] = "12A"
        obj["address_audit"] = {"status": "verified", "sources": [{"reference": "private"}]}
        url = f"/api/admin/objects/{obj['id']}"; headers = self.case.admin_headers()
        response = await self.client.patch(url, headers=headers, json={"name": "Nowa"})
        self.assertEqual((await response.json())["object"]["house_number"], "12A")
        response = await self.client.patch(url, headers=headers, json={"house_number": None})
        changed = (await response.json())["object"]
        self.assertIsNone(changed["house_number"])
        self.assertEqual(changed["address_audit"]["status"], "requires_review")
        self.assertTrue(changed["location_audit"]["requires_review"])
        persisted = app.load_objects(self.case.app[app.OBJECTS_PATH_KEY], [])
        self.assertEqual(persisted[0]["address_audit"], changed["address_audit"])
        for value in [True, 12, [], "12-20"]:
            response = await self.client.patch(url, headers=headers, json={"house_number": value})
            self.assertEqual(response.status, 400)
        response = await self.client.patch(url, headers=headers, json={"address_audit": {"status": "verified"}})
        self.assertEqual(response.status, 400)

    async def test_post_public_api_and_no_forged_audit(self):
        headers = self.case.admin_headers()
        response = await self.client.post("/api/admin/objects", headers=headers,
            json={"city_sym": "0765432", "house_number": "12a"})
        self.assertEqual(response.status, 201)
        obj = (await response.json())["object"]
        self.assertEqual(obj["house_number"], "12A")
        response = await self.client.get(f"/api/v1/objects/{obj['id']}")
        self.assertIn('12A', await response.text())
        response = await self.client.post("/api/admin/objects", headers=headers,
            json={"city_sym": "0765432", "address_audit": {"status": "verified"}})
        self.assertEqual(response.status, 400)
