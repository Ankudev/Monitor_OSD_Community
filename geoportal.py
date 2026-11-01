"""Porównanie z PRG. Zgodny rekord nie jest dowodem adresu ani zasilania obiektu."""

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from address_matching import address_binding, house_number
from geocoder import Geocoder, GeocoderHttpError, GeocoderProviderError
from geometry import normalize_geometry, geometry_contains
from location_validation import coordinates

VERSION = 1
BASE = "https://ogcapi.geoportal.gov.pl/res/ims/maps"
CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
DATASETS = {"address": ("prg_ad", "PunktAdresowy"), "commune": ("PRG", "GranicaGminy")}
SOURCE = "GUGiK / Geoportal — PRG"


def now():
    return datetime.now(timezone.utc).isoformat()


def distance(lat, lon, y, x):
    a = math.sin(math.radians(y-lat)/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(y))*math.sin(math.radians(x-lon)/2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, a)))


def bbox_for(lat, lon, radius):
    dy = math.degrees(radius / 6371000)
    dx = dy / max(0.01, math.cos(math.radians(lat)))
    return [lon-dx, lat-dy, lon+dx, lat+dy]


def code(value, length):
    if not isinstance(value, str) or not re.fullmatch(r"\d{" + str(length) + "}", value):
        raise GeocoderProviderError("PRG zwrócił niepoprawny kod identyfikacyjny")
    return value


def metadata(value, collection):
    if not isinstance(value, dict) or value.get("id") != collection or value.get("crs") != [CRS84]:
        raise GeocoderProviderError("Nieznany kontrakt lub układ współrzędnych PRG")


def page_valid(value, kind):
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection" or not isinstance(value.get("features"), list):
        raise GeocoderProviderError("PRG nie zwrócił kolekcji GeoJSON")
    if "crs" in value:
        raise GeocoderProviderError("Nieoczekiwany CRS w GeoJSON PRG")
    for key in ("numberReturned", "numberMatched"):
        if isinstance(value.get(key), bool) or not isinstance(value.get(key), int) or value[key] < 0:
            raise GeocoderProviderError("Brak poprawnych liczników PRG")
    if value["numberReturned"] != len(value["features"]) or value["numberReturned"] > 100:
        raise GeocoderProviderError("Niezgodne liczniki strony PRG")
    if not isinstance(value.get("links"), list):
        raise GeocoderProviderError("Brak listy stron PRG")
    for feature in value["features"]:
        if not isinstance(feature, dict) or feature.get("type") != "Feature" or not isinstance(feature.get("id"), str) or not feature["id"]:
            raise GeocoderProviderError("Niepoprawny rekord PRG")
        props = feature.get("properties")
        if not isinstance(props, dict):
            raise GeocoderProviderError("Brak danych rekordu PRG")
        if kind == "address":
            shape = feature.get("geometry") or {}
            xy = shape.get("coordinates")
            if shape.get("type") != "Point" or not isinstance(xy, list) or len(xy) != 2 or any(isinstance(n, bool) or not isinstance(n, (float, int)) for n in xy):
                raise GeocoderProviderError("Niepoprawny punkt adresowy PRG")
            lat, lon = coordinates(xy[1], xy[0])
            if lat is None or lon is None:
                raise GeocoderProviderError("Brak współrzędnych PRG")
            code(props.get("idSIMCNazwyMiejscowosci"), 7)
            if props.get("idULICNazwyUlicy") not in (None, ""):
                code(props["idULICNazwyUlicy"], 5)
            code(props.get("terytGminy"), 7)
            if not isinstance(props.get("numerPorzadkowy"), str) or not props["numerPorzadkowy"].strip():
                raise GeocoderProviderError("Brak numeru punktu adresowego PRG")
        else:
            normalize_geometry(feature.get("geometry"))
            code(props.get("kod"), 7)


def next_params(value, dataset, collection, bbox, offset):
    links = [link for link in value["links"] if isinstance(link, dict) and link.get("rel") == "next"]
    if not links:
        return None
    if len(links) != 1 or not isinstance(links[0].get("href"), str):
        raise GeocoderProviderError("Niepoprawna następna strona PRG")
    url = urlsplit(links[0]["href"])
    expected = f"/res/ims/maps/{dataset}/apiFeatures/collections/{collection}/items"
    if url.scheme != "https" or url.netloc != "ogcapi.geoportal.gov.pl" or url.path != expected or url.fragment:
        raise GeocoderProviderError("Niedozwolony link strony PRG")
    query = parse_qs(url.query, keep_blank_values=True)
    if set(query) - {"f", "offset", "limit", "bbox"} or any(len(v) != 1 for v in query.values()):
        raise GeocoderProviderError("Niepoprawne parametry strony PRG")
    try:
        start = int(query["offset"][0])
        limit = int(query["limit"][0])
        same_bbox = [float(v) for v in query["bbox"][0].split(",")]
    except (ValueError, KeyError) as error:
        raise GeocoderProviderError("Niepoprawna paginacja PRG") from error
    if start != offset + value["numberReturned"] or limit != 100 or same_bbox != bbox or query.get("f") != ["json"]:
        raise GeocoderProviderError("Zmiana zakresu lub pętla paginacji PRG")
    return start


def boundary_distance(lat, lon, shape):
    polygons = [shape["coordinates"]] if shape["type"] == "Polygon" else shape["coordinates"]
    scale_x = math.cos(math.radians(lat))*111195
    scale_y = 111195
    best = math.inf
    for polygon in polygons:
        for ring in polygon:
            for a, b in zip(ring, ring[1:]):
                ax, ay = (a[0]-lon)*scale_x, (a[1]-lat)*scale_y
                bx, by = (b[0]-lon)*scale_x, (b[1]-lat)*scale_y
                dx, dy = bx-ax, by-ay
                t = max(0, min(1, -(ax*dx+ay*dy)/(dx*dx+dy*dy))) if dx or dy else 0
                best = min(best, math.hypot(ax+t*dx, ay+t*dy))
    return best


class Geoportal:
    def __init__(self, session, cache_path, user_agent, *, min_interval=1, client=None):
        self.client = client or Geocoder(session, cache_path, provider_url=BASE,
            user_agent=user_agent, timeout_seconds=15, extra_params={},
            max_response_bytes=8*1024*1024, persist_cooldown=True,
            min_interval_seconds=min_interval, shield_requests=False)
        self._tasks = {}
        self._guard = asyncio.Lock()

    async def start(self):
        await self.client.start()

    async def close(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()

    async def compare(self, obj, expected_commune=None, radius=300):
        if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 1 <= radius <= 1000:
            raise ValueError("Promień porównania musi wynosić od 1 do 1000 m")
        key = json.dumps([address_binding(obj), expected_commune, radius], sort_keys=True)
        async with self._guard:
            if key not in self._tasks:
                if len(self._tasks) >= 20:
                    from geocoder import GeocoderBusyError
                    raise GeocoderBusyError("Geoportal ma zbyt wiele oczekujących porównań")
                self._tasks[key] = asyncio.create_task(self._timed_compare(obj, expected_commune, radius))
                self._tasks[key].add_done_callback(lambda task, k=key: self._finished(k, task))
            task = self._tasks[key]
        return await asyncio.shield(task)

    def _finished(self, key, task):
        self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _timed_compare(self, obj, expected_commune, radius):
        async with asyncio.timeout(60):
            return await self._compare(obj, expected_commune, radius)

    async def _compare(self, obj, expected_commune, radius):
        report = {"version": VERSION, "object_id": str(obj["id"]), "binding": address_binding(obj),
            "checked_at": now(), "status": "inconclusive", "truncated": False,
            "radius_m": radius, "source": SOURCE, "source_url": "https://www.geoportal.gov.pl/",
            "address": {"status": "unknown", "candidates": [], "object_address_confirmed": False},
            "commune": {"status": "unknown", "expected_code": expected_commune, "candidates": []},
            "reasons": [], "evidence": []}
        lat, lon = coordinates(obj.get("latitude"), obj.get("longitude"))
        if lat is None:
            report["reasons"].append("Brak współrzędnych do porównania")
            return report, []
        if not 49 <= lat <= 55.1 or not 14 <= lon <= 24.6:
            report["reasons"].append("Punkt poza obszarem roboczym Polski; sprawdź kolejność GPS")
            return report, []
        budget = {"requests": 0, "bytes": 0}
        evidence = []
        points, complete_a = await self._collection("address", bbox_for(lat, lon, radius), budget, evidence)
        borders, complete_g = await self._collection("commune", bbox_for(lat, lon, 6), budget, evidence)
        report["truncated"] = not (complete_a and complete_g)
        bbox = bbox_for(lat, lon, radius)
        selected = []
        for feature in points:
            x, y = feature["geometry"]["coordinates"]
            d = distance(lat, lon, y, x)
            if not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3] and d <= radius):
                continue
            p = feature["properties"]
            try:
                number = house_number(p["numerPorzadkowy"])
            except ValueError:
                number = None
            simc = p["idSIMCNazwyMiejscowosci"] == obj.get("city_sym")
            ulic = bool(obj.get("street_sym")) and p.get("idULICNazwyUlicy") == obj.get("street_sym")
            no_street = not obj.get("street_sym") and not p.get("idULICNazwyUlicy")
            num = bool(obj.get("house_number")) and number == house_number(obj.get("house_number"))
            selected.append({"feature_id": feature["id"], "source_version": p.get("idWersja"),
                "city": p.get("nazwaMiejscowosci"), "city_sym": p["idSIMCNazwyMiejscowosci"],
                "street": p.get("nazwaUlicy"), "street_sym": p.get("idULICNazwyUlicy"),
                "house_number": p["numerPorzadkowy"], "commune_code": p["terytGminy"],
                "longitude": x, "latitude": y, "distance_m": round(d, 1),
                "same_city": simc, "same_street": ulic or no_street, "same_number": num,
                "address_equal": simc and (ulic or no_street) and num})
        selected.sort(key=lambda p: (not p["address_equal"], not p["same_city"], p["distance_m"]))
        exact = [p for p in selected if p["address_equal"]]
        report["address"].update(candidates=selected[:10], total_candidates=len(selected), status="consistent" if len(exact) == 1 and complete_a else "unknown")
        report["reasons"].append("Adres zgodny z rekordem PRG. Powiązanie z obiektem wymaga potwierdzenia." if report["address"]["status"] == "consistent" else "Nie znaleziono jednoznacznej zgodności adresu w sprawdzonym obszarze")
        near_boundary = False
        contained = []
        for feature in borders:
            shape = normalize_geometry(feature["geometry"])
            near = boundary_distance(lat, lon, shape) <= 5
            inside = geometry_contains(lat, lon, shape)
            near_boundary |= near
            if inside or near:
                p = feature["properties"]
                candidate = {"code": p["kod"], "name": p.get("nazwaJednostki"), "contains": inside,
                    "near_boundary": near, "feature_id": feature["id"], "source_version": p.get("idWersja")}
                report["commune"]["candidates"].append(candidate)
                if inside:
                    contained.append(candidate)
        if complete_g and not near_boundary and len(contained) == 1:
            actual = contained[0]["code"]
            if expected_commune and re.fullmatch(r"\d{7}", expected_commune):
                report["commune"]["status"] = "consistent" if actual == expected_commune else "different"
            elif expected_commune and re.fullmatch(r"\d{6}", expected_commune) and actual[:6] != expected_commune:
                report["commune"]["status"] = "different"
            else:
                report["reasons"].append("Brak pełnego kodu gminy w lokalnym TERYT; zgodności gminy nie zatwierdzono")
        else:
            report["reasons"].append("Przynależność do gminy niejednoznaczna lub punkt przy granicy")
        report["status"] = "needs_review" if report["commune"]["status"] == "different" else "consistent" if report["address"]["status"] == report["commune"]["status"] == "consistent" and not report["truncated"] else "inconclusive"
        if report["truncated"]:
            report["address"]["status"] = "unknown"
            report["status"] = "inconclusive"
            report["reasons"].append("Wynik niepełny. Osiągnięto limit lub dane stron są niespójne.")
        report["request_count"] = budget["requests"]
        for item in evidence:
            report["evidence"].append({key: item[key] for key in ("sha256", "url", "source_fetched_at", "observed_at")})
        return report, evidence

    async def _request(self, endpoint, params, validator, budget, evidence, *, meta=False):
        for attempt in range(2):
            if budget["requests"] >= 12:
                raise GeocoderProviderError("Osiągnięto budżet porównania PRG")
            budget["requests"] += 1
            try:
                value = await self.client.request_json(endpoint, params, validator=validator,
                    empty=(lambda v: not v["features"]) if not meta else None, cache_ttl=604800 if meta else None)
                break
            except (GeocoderHttpError, asyncio.TimeoutError) as error:
                if attempt or isinstance(error, GeocoderHttpError) and error.status < 500:
                    raise
                await asyncio.sleep(2)
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        budget["bytes"] += len(body)
        if len(body) > 8*1024*1024 or budget["bytes"] > 16*1024*1024:
            raise GeocoderProviderError("Odpowiedź przekracza budżet porównania PRG")
        from urllib.parse import urlencode
        fetched = await self.client.cached_at(endpoint, params) if hasattr(self.client, "cached_at") else None
        evidence.append({"body": value, "sha256": hashlib.sha256(body).hexdigest(),
            "url": BASE + "/" + endpoint + "?" + urlencode(params), "observed_at": now(),
            "source_fetched_at": datetime.fromtimestamp(fetched, timezone.utc).isoformat() if fetched else None})
        return value

    async def _collection(self, kind, bbox, budget, evidence):
        dataset, collection = DATASETS[kind]
        path = f"{dataset}/apiFeatures/collections/{collection}"
        await self._request(path, {"f": "json"}, lambda v: metadata(v, collection), budget, evidence, meta=True)
        features, ids, first_count, complete = [], set(), None, True
        offset = 0
        for _ in range(5):
            params = {"f": "json", "limit": "100", "offset": str(offset), "bbox": ",".join(map(str, bbox))}
            value = await self._request(path + "/items", params, lambda v: page_valid(v, kind), budget, evidence)
            if first_count is None:
                first_count = value["numberMatched"]
            complete &= first_count == value["numberMatched"]
            for feature in value["features"]:
                if feature["id"] in ids:
                    complete = False
                else:
                    features.append(feature)
                    ids.add(feature["id"])
            following = next_params(value, dataset, collection, bbox, offset)
            if following is None:
                return features, complete and len(ids) == first_count
            offset = following
        return features, False
