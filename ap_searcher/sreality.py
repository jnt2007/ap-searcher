"""sreality.cz access layer.

The site no longer exposes a usable public JSON API (v2 is gone, v1 returns 401),
but it is a Next.js app: every search and detail page ships the complete,
already-structured payload inside <script id="__NEXT_DATA__">. We fetch the HTML
and read that JSON — no HTML scraping, no JS execution.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .amenities import fold

BASE = "https://www.sreality.cz"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
AREA_RE = re.compile(r"(\d[\d\s]*)\s*m²")

# categoryMainCb
KIND_CODES = {"byty": 1, "domy": 2, "pozemky": 3, "komercni": 4, "ostatni": 5}
# categoryTypeCb
DEAL_CODES = {"prodej": 1, "pronajem": 2, "drazby": 3}
# categorySubCb, as used by the ?velikost= filter
DISPOSITION_CODES = {
    "1+kk": 2, "1+1": 3, "2+kk": 4, "2+1": 5, "3+kk": 6, "3+1": 7,
    "4+kk": 8, "4+1": 9, "5+kk": 10, "5+1": 11, "6-a-vice": 12, "atypicky": 16,
}
KIND_SINGULAR = {"byty": "byt", "domy": "dum", "pozemky": "pozemek"}

# params.furnished enum. 0 ("- vyber vybavení") means the advertiser left it blank,
# which is common - about a third of ads - and is kept distinct from an explicit "Ne".
FURNISHED_STATES = {1: "yes", 2: "no", 3: "partly"}
FURNISHED_CODES = {name: code for code, name in FURNISHED_STATES.items()}
# The ?vybaveni= search filter takes codebook names, never the numbers above - a
# numeric value is silently ignored. The numbers are what comes back in the queryHash.
FURNISHED_QUERY = {"yes": "vybaveny", "no": "nevybaveny", "partly": "castecne"}

# The image CDN only serves whitelisted transforms; this is the thumbnail preset the
# search page itself uses. Without it the bare image URL answers 401.
IMAGE_TRANSFORM = "fl=res,100,100,1|jpg,80"


class ParseError(RuntimeError):
    """The page did not contain the payload we expect — sreality changed shape."""


@dataclass
class SearchPage:
    results: list[dict[str, Any]]
    total: int
    query_key: dict[str, Any]
    limit: int = 22
    warnings: list[str] = field(default_factory=list)


def build_search_url(profile, page: int = 1, area=None) -> str:
    """Human-readable search URL; sreality echoes the parsed filters back to us.

    One URL covers one area; a profile with several sweeps each of them in turn.
    """
    area = area or profile.search_areas[0]
    path = f"/hledani/{profile.deal}/{profile.kind}/{area.locality}"
    params: list[tuple[str, str]] = []
    if profile.dispositions:
        params.append(("velikost", ",".join(profile.dispositions)))
    if profile.price_from:
        params.append(("cena-od", str(profile.price_from)))
    if profile.price_to:
        params.append(("cena-do", str(profile.price_to)))
    if profile.area_from:
        params.append(("plocha-od", str(profile.area_from)))
    if profile.area_to:
        params.append(("plocha-do", str(profile.area_to)))
    if profile.furnished:
        params.append(("vybaveni", ",".join(FURNISHED_QUERY[f] for f in profile.furnished)))
    if profile.has_bbox:
        # Map bounding box; sreality accepts these on the Czech URLs too.
        params.append(("lat-min", repr(profile.lat_min)))
        params.append(("lat-max", repr(profile.lat_max)))
        params.append(("lon-min", repr(profile.lon_min)))
        params.append(("lon-max", repr(profile.lon_max)))
    if page > 1:
        params.append(("strana", str(page)))
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{BASE}{path}" + (f"?{query}" if query else "")


def parse_next_data(html: str) -> dict[str, Any]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ParseError("no __NEXT_DATA__ script found")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ParseError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc


def _query(data: dict[str, Any], prefix: str) -> dict[str, Any]:
    queries = (
        data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
    )
    for q in queries:
        if str(q.get("queryHash", "")).startswith(prefix):
            return q
    raise ParseError(f"no {prefix} query in page payload")


def extract_search(html: str) -> SearchPage:
    data = parse_next_data(html)
    q = _query(data, '["estatesSearch"')
    payload = q["state"].get("data") or {}
    key = json.loads(q["queryHash"])[1]
    pagination = payload.get("pagination") or {}
    promo_ids = set()
    for tip_key in ("brokerTip", "regionTip", "projectTip"):
        tip = payload.get(tip_key)
        for item in tip.get("results", []) if isinstance(tip, dict) else []:
            promo_ids.add(item.get("id"))
    results = [r for r in payload.get("results", []) if r.get("id") not in promo_ids]
    return SearchPage(
        results=results,
        total=int(pagination.get("total") or 0),
        query_key=key,
        limit=int(pagination.get("limit") or 22),
    )


def check_filters(profile, key: dict[str, Any], area=None) -> list[str]:
    """Compare what we asked for against what sreality says it applied.

    Silently-ignored filters (a renamed query parameter) would otherwise produce a
    plausible-looking but wrong result set.
    """
    problems: list[str] = []

    def want(field_name: str, expected, actual) -> None:
        if expected is not None and actual != expected:
            problems.append(f"{field_name}: asked {expected!r}, site applied {actual!r}")

    want("kind", [KIND_CODES.get(profile.kind)], key.get("categoryMainCb"))
    want("deal", [DEAL_CODES.get(profile.deal)], key.get("categoryTypeCb"))
    want("price_from", profile.price_from, key.get("priceFrom"))
    want("price_to", profile.price_to, key.get("priceTo"))
    want("area_from", profile.area_from, key.get("usableAreaFrom"))
    want("area_to", profile.area_to, key.get("usableAreaTo"))
    if profile.dispositions:
        expected = sorted(DISPOSITION_CODES[d] for d in profile.dispositions if d in DISPOSITION_CODES)
        actual = sorted(key.get("categorySubCb") or [])
        if expected != actual:
            problems.append(f"dispositions: asked {expected}, site applied {actual}")
    if profile.furnished:
        expected = sorted(FURNISHED_CODES[f] for f in profile.furnished)
        actual = sorted(key.get("furnished") or [])
        if expected != actual:
            problems.append(f"furnished: asked {expected}, site applied {actual}")
    if profile.has_bbox:
        for field_name, key_name in (
            ("lat_min", "latMin"), ("lat_max", "latMax"),
            ("lon_min", "lonMin"), ("lon_max", "lonMax"),
        ):
            expected = getattr(profile, field_name)
            actual = key.get(key_name)
            if actual is None or abs(float(actual) - expected) > 1e-9:
                problems.append(f"{field_name}: asked {expected!r}, site applied {actual!r}")

    area = area or profile.search_areas[0]
    locality_keys = ("localityRegionId", "localityDistrictId", "localityMunicipalityId", "localityWardId")
    if not any(key.get(k) for k in locality_keys):
        problems.append(f"locality {area.locality!r} was not recognised by the site")
    return problems


def _area_from_name(name: str | None) -> float | None:
    if not name:
        return None
    m = AREA_RE.search(name)
    if not m:
        return None
    return float(m.group(1).replace(" ", "").replace("\xa0", ""))


def detail_url(result: dict[str, Any]) -> str:
    loc = result.get("locality") or {}
    kind = KIND_SINGULAR.get(
        {1: "byty", 2: "domy", 3: "pozemky"}.get((result.get("categoryMainCb") or {}).get("value"), ""),
        "byt",
    )
    deal = {1: "prodej", 2: "pronajem", 3: "drazby"}.get(
        (result.get("categoryTypeCb") or {}).get("value"), "pronajem"
    )
    disposition = (result.get("categorySubCb") or {}).get("name") or "x"
    slug_parts = [
        loc.get("citySeoName"),
        loc.get("cityPartSeoName") or loc.get("quarterSeoName"),
        loc.get("streetSeoName"),
    ]
    slug = "-".join(p for p in slug_parts if p) or "x"
    path = f"/detail/{deal}/{kind}/{disposition}/{slug}/{result['id']}"
    # A stale slug still resolves: sreality 301s to the canonical URL.
    return BASE + urllib.parse.quote(path, safe="/+")


def listing_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten a search result into the shape the store expects."""
    loc = result.get("locality") or {}
    images = result.get("images") or []
    image = images[0].get("url") if images else None
    if image:
        if image.startswith("//"):
            image = "https:" + image
        if "?" not in image:
            image = f"{image}?{IMAGE_TRANSFORM}"
    return {
        "id": result["id"],
        "title": result.get("name"),
        "deal": (result.get("categoryTypeCb") or {}).get("name"),
        "disposition": (result.get("categorySubCb") or {}).get("name"),
        "price_czk": result.get("priceCzk") or result.get("priceSummaryCzk"),
        "price_unit": (result.get("priceSummaryUnitCb") or {}).get("name"),
        "price_per_m2": result.get("priceCzkPerSqM"),
        "area_m2": _area_from_name(result.get("name")),
        "city": loc.get("city"),
        "city_part": loc.get("cityPart") or loc.get("quarter"),
        "district": loc.get("district"),
        "street": " ".join(x for x in (loc.get("street"), loc.get("streetNumber")) if x) or None,
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "url": detail_url(result),
        "image_url": image,
        "raw_json": json.dumps(result, ensure_ascii=False),
    }


def neighborhood_slug(name: str) -> str:
    """'Vysočany' -> 'vysocany', matching sreality's own cityPartSeoName spelling."""
    return re.sub(r"[^a-z0-9]+", "-", fold(name).lower()).strip("-")


def matches_neighborhoods(result: dict[str, Any], slugs: set[str]) -> bool:
    """Neighbourhood filtering is ours, not the site's.

    sreality's listing search has no ward/city-part filter: the `ctvrt` query
    parameter exists only on the price-map route and is silently dropped here, so the
    profile is scoped to the containing district and the results are filtered on the
    `cityPart` the payload already carries.
    """
    if not slugs:
        return True
    loc = result.get("locality") or {}
    candidates = {
        loc.get("cityPartSeoName"),
        loc.get("quarterSeoName"),
        neighborhood_slug(loc.get("cityPart") or ""),
        neighborhood_slug(loc.get("quarter") or ""),
    }
    return bool(slugs & {c for c in candidates if c})


def furnished_state(params: dict[str, Any]) -> str:
    """'yes' | 'partly' | 'no' | 'unknown' - never None, so a derived blank is
    distinguishable from a listing whose detail has not been read yet."""
    value = (params.get("furnished") or {}).get("value")
    return FURNISHED_STATES.get(value, "unknown")


def extract_detail(html: str) -> dict[str, Any]:
    data = parse_next_data(html)
    q = _query(data, '["estate"')
    estate = q["state"].get("data") or {}
    params = estate.get("params") or {}
    transport = (estate.get("extendedPois") or {}).get("transport") or {}
    return {
        "description": estate.get("description") or "",
        "params": params,
        "transport": transport.get("values") or [],
        "area_m2": params.get("usableArea"),
        "price_note": params.get("priceNote"),
        # "since" is the publication date, "edited" the last change. Both are plain
        # YYYY-MM-DD strings and present on every listing seen so far.
        "published_on": params.get("since") or None,
        "edited_on": params.get("edited") or None,
        "furnished": furnished_state(params),
    }
