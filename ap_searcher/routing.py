"""Commute routing via MOTIS (Transitous public instance by default).

Transitous is free and needs no API key, but it is volunteer-run: the User-Agent
must identify us with a contact address, calls are spaced out, and a per-run
request budget plus the routes cache keep the volume low. Point `routing.base_url`
at a self-hosted MOTIS container to remove the limits entirely.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import urllib.parse
from dataclasses import dataclass
from typing import Any

WALK = "walk"
TRANSIT = "transit"

# Walking does not depend on the timetable, so its cache entries are not tied to an
# arrival moment - otherwise every walk would be recomputed when the arrival date
# rolls to the next week, for an answer that cannot have changed.
TIMELESS = "any"

MODE_LABELS = {
    "WALK": "walk",
    "BIKE": "bike",
    "CAR": "car",
    "BUS": "bus",
    "COACH": "coach",
    "SUBWAY": "metro",
    "METRO": "metro",
    "TRAM": "tram",
    "RAIL": "train",
    "HIGHSPEED_RAIL": "train",
    "REGIONAL_RAIL": "train",
    "REGIONAL_FAST_RAIL": "train",
    "LONG_DISTANCE": "train",
    "NIGHT_RAIL": "train",
    "FERRY": "ferry",
    "TROLLEYBUS": "trolley",
    "FUNICULAR": "funicular",
    "CABLE_CAR": "cable car",
}


class RoutingError(RuntimeError):
    pass


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def stamp_for(mode: str, arrive_by: str) -> str:
    """Transit routes are specific to an arrival moment; walking routes are not."""
    return arrive_by if mode == TRANSIT else TIMELESS


def route_key(lat: float, lon: float, office, mode: str, arrive_by: str) -> str:
    """Rounded to ~1 m so near-identical coordinates share a cached route."""
    stamp = stamp_for(mode, arrive_by)
    return f"{lat:.5f},{lon:.5f}|{office.lat:.5f},{office.lon:.5f}|{mode}|{stamp}"


@dataclass
class Router:
    fetcher: Any            # http.Fetcher with use_cache=False
    base_url: str = "https://api.transitous.org"
    max_walk_s: int = 10800

    def _plan(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/api/v1/plan?" + urllib.parse.urlencode(params)
        body = self.fetcher.get(url, max_age_s=0, force=True)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RoutingError(f"MOTIS returned non-JSON: {body[:200]}") from exc

    def geocode(self, text: str, near: tuple[float, float] | None = None) -> dict[str, Any] | None:
        params = {"text": text}
        if near:
            params["place"] = f"{near[0]},{near[1]}"
        url = f"{self.base_url.rstrip('/')}/api/v1/geocode?" + urllib.parse.urlencode(params)
        results = json.loads(self.fetcher.get(url, max_age_s=0, force=True))
        return results[0] if results else None

    def transit(self, lat: float, lon: float, office, arrive: dt.datetime) -> dict[str, Any]:
        data = self._plan(
            {
                "fromPlace": f"{lat},{lon}",
                "toPlace": f"{office.lat},{office.lon}",
                "time": _iso_utc(arrive),
                "arriveBy": "true",
                "numItineraries": 2,
            }
        )
        itineraries = data.get("itineraries") or []
        if not itineraries:
            return {"status": "none"}
        best = min(itineraries, key=lambda i: i.get("duration") or 1e9)
        return _summarise(best)

    def walk(self, lat: float, lon: float, office, arrive: dt.datetime) -> dict[str, Any]:
        data = self._plan(
            {
                "fromPlace": f"{lat},{lon}",
                "toPlace": f"{office.lat},{office.lon}",
                "time": _iso_utc(arrive),
                "arriveBy": "true",
                "directModes": "WALK",
                "transitModes": "",
                # the 1 h default silently returns nothing for longer walks
                "maxDirectTime": self.max_walk_s,
            }
        )
        direct = data.get("direct") or []
        if not direct:
            return {"status": "none"}
        best = min(direct, key=lambda i: i.get("duration") or 1e9)
        return _summarise(best)


def _iso_utc(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarise(itinerary: dict[str, Any]) -> dict[str, Any]:
    legs = []
    parts = []
    for leg in itinerary.get("legs") or []:
        mode = MODE_LABELS.get(leg.get("mode", ""), (leg.get("mode") or "?").lower())
        minutes = round((leg.get("duration") or 0) / 60)
        line = leg.get("routeShortName")
        legs.append(
            {
                "mode": mode,
                "line": line,
                "minutes": minutes,
                "from": (leg.get("from") or {}).get("name"),
                "to": (leg.get("to") or {}).get("name"),
                "distance_m": leg.get("distance"),
            }
        )
        if mode == "walk":
            if minutes >= 1:
                parts.append(f"walk {minutes}′")
        else:
            parts.append(f"{mode} {line}".strip())
    return {
        "status": "ok",
        "duration_s": int(itinerary.get("duration") or 0),
        "transfers": itinerary.get("transfers"),
        "summary": " · ".join(parts),
        "legs": legs,
    }
