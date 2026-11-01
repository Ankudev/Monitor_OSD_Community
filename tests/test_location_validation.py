import json
import unittest
from copy import deepcopy

import app
import test_app as base
from location_validation import coordinates, validate_object_list


class LocationValidationUnitTests(unittest.TestCase):
    def test_coordinate_matrix(self):
        accepted = [
            (52, 21, (52.0, 21.0)),
            ("52.1", "21.2", (52.1, 21.2)),
            (None, None, (None, None)),
            ("", " ", (None, None)),
        ]
        for latitude, longitude, expected in accepted:
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertEqual(coordinates(latitude, longitude), expected)

        rejected = [
            (True, 21), (52, False), ("NaN", 21), (52, "Infinity"),
            ([], 21), (52, {}), (91, 21), (52, 181), (52, None),
        ]
        for latitude, longitude in rejected:
            with self.subTest(latitude=latitude, longitude=longitude):
                with self.assertRaises(ValueError):
                    coordinates(latitude, longitude)

    def test_inventory_error_names_object_and_duplicate(self):
        with self.assertRaisesRegex(ValueError, "site-a.*szerokość"):
            validate_object_list([{"id": "site-a", "city_sym": "0123456", "latitude": True, "longitude": 21}])
        with self.assertRaisesRegex(ValueError, "site-a.*powtórzony"):
            validate_object_list([
                {"id": "site-a", "city_sym": "0123456"},
                {"id": "site-a", "city_sym": "0765432"},
            ])


class LocationValidationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = base.ApiTests("test_status_endpoint")
        await self.case.asyncSetUp()
        self.client = self.case.client
        self.application = self.case.app

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_post_and_patch_reject_invalid_gps_without_mutation(self):
        headers = self.case.admin_headers()
        original_memory = deepcopy(self.application[app.CONFIG_KEY]["teryt_locations"])
        objects_path = self.application[app.OBJECTS_PATH_KEY]
        original_disk = objects_path.read_bytes() if objects_path.exists() else None
        bad_values = [True, False, "NaN", "Infinity", [], {}, 91]
        for value in bad_values:
            with self.subTest(value=value):
                created = await self.client.post(
                    "/api/objects", headers=headers,
                    json={"city_sym": "0765432", "latitude": value, "longitude": 21},
                )
                self.assertEqual(created.status, 400)
        object_id = original_memory[0]["id"]
        patched = await self.client.patch(
            f"/api/admin/objects/{object_id}", headers=headers,
            json={"latitude": 52, "longitude": 181},
        )
        self.assertEqual(patched.status, 400)
        self.assertEqual(self.application[app.CONFIG_KEY]["teryt_locations"], original_memory)
        self.assertEqual(objects_path.read_bytes() if objects_path.exists() else None, original_disk)

    async def test_both_empty_coordinates_are_allowed(self):
        response = await self.client.post(
            "/api/objects", headers=self.case.admin_headers(),
            json={"city_sym": "0765432", "latitude": "", "longitude": None},
        )
        self.assertEqual(response.status, 201)
        item = (await response.json())["object"]
        self.assertIsNone(item["latitude"])
        self.assertIsNone(item["longitude"])

    async def test_location_edit_preserves_audit_provenance_and_id(self):
        current = self.application[app.CONFIG_KEY]["teryt_locations"][0]
        current["location_audit"] = {
            "radio_id": "123", "source_url": "https://example.test/123",
            "checked_at": "2026-09-01T10:00:00Z", "requires_review": False,
        }
        object_id = current["id"]
        response = await self.client.patch(
            f"/api/admin/objects/{object_id}", headers=self.case.admin_headers(),
            json={"latitude": 52.2, "longitude": 21.2},
        )
        self.assertEqual(response.status, 200)
        updated = (await response.json())["object"]
        self.assertEqual(updated["id"], object_id)
        self.assertEqual(updated["location_audit"]["radio_id"], "123")
        self.assertEqual(updated["location_audit"]["source_url"], "https://example.test/123")
        self.assertTrue(updated["location_audit"]["requires_review"])
        self.assertEqual(updated["location_audit"]["review_reason"], "location_changed")
        self.assertTrue(updated["location_audit"]["invalidated_at"])

    async def test_name_edit_keeps_valid_audit_unchanged(self):
        current = self.application[app.CONFIG_KEY]["teryt_locations"][0]
        current["location_audit"] = {"radio_id": "123", "requires_review": False}
        before = deepcopy(current["location_audit"])
        response = await self.client.patch(
            f"/api/admin/objects/{current['id']}", headers=self.case.admin_headers(),
            json={"name": "Nowa nazwa"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["object"]["location_audit"], before)

    async def test_reverse_uses_same_strict_validation(self):
        for query in ("lat=true&lon=21", "lat=NaN&lon=21", "lat=52&lon=181", "lat=52"):
            with self.subTest(query=query):
                response = await self.client.get("/api/location/reverse?" + query)
                self.assertEqual(response.status, 400)
