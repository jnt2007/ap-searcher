"""The run pipeline: search → diff → details → amenities → routing.

Written as plain functions driven by a progress callback and a cancel flag so the
same code can be called from the background worker or from a test.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import amenities as amen
from . import routing, sreality
from .cache import Store
from .config import Config
from .http import BudgetExceeded, Fetcher

log = logging.getLogger(__name__)

Progress = Callable[..., None]


@dataclass
class RunOptions:
    profile: str | list[str] | None = None   # None/empty = every profile
    max_pages: int | None = None        # overrides the profile setting
    do_search: bool = True
    do_details: bool = True
    do_routing: bool = True
    force_refresh: bool = False
    routing_budget: int | None = None

    def profile_names(self) -> list[str]:
        """A comma-separated string and a list mean the same thing."""
        if not self.profile:
            return []
        names = self.profile.split(",") if isinstance(self.profile, str) else self.profile
        return [n.strip() for n in names if n and n.strip()]


@dataclass
class RunResult:
    stages: list[str] = field(default_factory=list)
    new_listings: list[int] = field(default_factory=list)
    price_changes: list[dict] = field(default_factory=list)
    removed: int = 0
    details_fetched: int = 0
    details_cached: int = 0
    routes_computed: int = 0
    routes_cached: int = 0
    warnings: list[str] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": self.stages,
            "new_listings": self.new_listings,
            "new_count": len(self.new_listings),
            "price_changes": self.price_changes,
            "removed": self.removed,
            "details_fetched": self.details_fetched,
            "details_cached": self.details_cached,
            "routes_computed": self.routes_computed,
            "routes_cached": self.routes_cached,
            "warnings": self.warnings,
            "counters": self.counters,
        }


class Cancelled(RuntimeError):
    pass


def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled by user")


def run(
    cfg: Config,
    opts: RunOptions,
    store: Store,
    progress: Progress = lambda **kw: None,
    cancel=None,
) -> RunResult:
    result = RunResult()
    profiles = [cfg.profile(n) for n in opts.profile_names()] or cfg.profiles

    sreality_fetcher = Fetcher(
        store=store,
        user_agent=cfg.user_agent,
        kind="sreality",
        min_interval_s=cfg.sreality.min_interval_s,
    )
    routing_fetcher = Fetcher(
        store=store,
        user_agent=cfg.user_agent,
        kind="routing",
        min_interval_s=cfg.routing.min_interval_s,
        use_cache=False,
        budget=opts.routing_budget or cfg.routing.max_requests_per_run,
    )

    try:
        if opts.do_search:
            _search(cfg, opts, store, profiles, sreality_fetcher, result, progress, cancel)
        if opts.do_details:
            _details(cfg, opts, store, profiles, sreality_fetcher, result, progress, cancel)
        if opts.do_routing:
            _routing(cfg, opts, store, profiles, routing_fetcher, result, progress, cancel)
    except Cancelled:
        result.warnings.append("run cancelled")
    except BudgetExceeded as exc:
        result.warnings.append(str(exc))

    result.counters = {
        "sreality": sreality_fetcher.counters.as_dict(),
        "routing": routing_fetcher.counters.as_dict(),
    }
    return result


def _scoped_listings(store: Store, profiles) -> list[Any]:
    seen: dict[int, Any] = {}
    for profile in profiles:
        for row in store.listings(profile.name):
            if row["removed_at"] is None:
                seen[row["id"]] = row
    return list(seen.values())


def _search(cfg, opts, store, profiles, fetcher, result, progress, cancel) -> None:
    result.stages.append("search")
    ttl = cfg.cache.search_ttl_h * 3600
    for profile in profiles:
        started = time.time()
        seen_ids: list[int] = []
        max_pages = opts.max_pages or profile.max_pages
        areas = profile.search_areas
        swept_whole_profile = True
        for area in areas:
            wanted = area.neighborhood_slugs
            kept = skipped = 0
            page = 1
            total = None
            while page <= max_pages:
                _check(cancel)
                url = sreality.build_search_url(profile, page, area)
                where = f"{profile.name}" if len(areas) == 1 else f"{profile.name}/{area.locality}"
                progress(
                    stage="search",
                    message=f"{where}: page {page}",
                    done=page - 1,
                    total=max_pages,
                )
                html = fetcher.get(url, max_age_s=ttl, force=opts.force_refresh)
                sp = sreality.extract_search(html)
                if page == 1:
                    total = sp.total
                    for problem in sreality.check_filters(profile, sp.query_key, area):
                        msg = f"{profile.name} ({area.locality}): filter mismatch — {problem}"
                        log.warning(msg)
                        result.warnings.append(msg)
                if not sp.results:
                    break
                for raw in sp.results:
                    if not sreality.matches_neighborhoods(raw, wanted):
                        skipped += 1
                        continue
                    listing = sreality.listing_from_result(raw)
                    flags = store.upsert_listing(listing, profile.name)
                    seen_ids.append(listing["id"])
                    kept += 1
                    if flags["is_new"]:
                        result.new_listings.append(listing["id"])
                    if flags["price_delta"]:
                        result.price_changes.append(
                            {"id": listing["id"], "delta": flags["price_delta"]}
                        )
                if total is not None and page * sp.limit >= total:
                    break
                page += 1
            if wanted:
                log.info(
                    "%s (%s): kept %d of %d results in %s",
                    profile.name, area.locality, kept, kept + skipped,
                    ", ".join(area.neighborhoods),
                )
            # Removal detection needs every area swept to the end, or a listing that
            # simply sat past the last fetched page would look like it had vanished.
            if total is None or total > max_pages * 22:
                swept_whole_profile = False
        if swept_whole_profile:
            result.removed += store.mark_removed(profile.name, seen_ids, started)


def _details(cfg, opts, store, profiles, fetcher, result, progress, cancel) -> None:
    result.stages.append("details")
    ttl = cfg.cache.detail_ttl_d * 86400
    listings = _scoped_listings(store, profiles)
    todo = [
        row for row in listings
        if opts.force_refresh or store.detail(row["id"], max_age_s=ttl) is None
    ]
    result.details_cached += len(listings) - len(todo)
    for i, row in enumerate(todo, 1):
        _check(cancel)
        progress(
            stage="details",
            message=f"{row['title'] or row['id']}",
            done=i - 1,
            total=len(todo),
        )
        try:
            html = fetcher.get(row["url"], max_age_s=ttl, force=opts.force_refresh)
            detail = sreality.extract_detail(html)
        except Exception as exc:  # network hiccup or a pulled listing
            result.warnings.append(f"detail {row['id']}: {type(exc).__name__}: {exc}")
            continue
        store.put_detail(row["id"], detail["description"], detail["params"], detail["transport"])
        store.set_dates(row["id"], detail.get("published_on"), detail.get("edited_on"))
        store.set_furnished(row["id"], detail["furnished"])
        if detail.get("area_m2"):
            store.conn.execute(
                "UPDATE listings SET area_m2 = ? WHERE id = ?", (detail["area_m2"], row["id"])
            )
            store.conn.commit()
        store.put_amenities(row["id"], amen.detect(detail["description"], detail["params"]))
        result.details_fetched += 1

    # Re-derive from already-cached details: amenity verdicts missing (never derived, or
    # a key added to amen.KEYS since the last run) or dates missing (detail cached by an
    # older version). No network.
    known = store.amenities_map()
    for row in listings:
        # A listing derived before a new amenity existed has rows for the old keys only,
        # so test for the whole key set rather than for the listing id - otherwise the
        # new column would read "?" until someone forced a full refresh.
        needs_amenities = opts.force_refresh or not set(amen.KEYS) <= known.get(row["id"], {}).keys()
        needs_dates = row["published_on"] is None
        needs_furnished = row["furnished"] is None
        if not (needs_amenities or needs_dates or needs_furnished):
            continue
        detail = store.detail(row["id"])
        if detail is None:
            continue
        params = json.loads(detail["params_json"] or "{}")
        if needs_amenities:
            store.put_amenities(row["id"], amen.detect(detail["description"], params))
        if needs_dates:
            store.set_dates(row["id"], params.get("since") or None, params.get("edited") or None)
        if needs_furnished:
            store.set_furnished(row["id"], sreality.furnished_state(params))


def _routing(cfg, opts, store, profiles, fetcher, result, progress, cancel) -> None:
    result.stages.append("routing")
    arrive = cfg.commute.next_arrival()
    arrive_key = arrive.isoformat()
    router = routing.Router(fetcher=fetcher, base_url=cfg.routing.base_url)
    listings = [r for r in _scoped_listings(store, profiles) if r["lat"] and r["lon"]]
    transit_ttl = cfg.cache.transit_ttl_d * 86400
    walk_ttl = cfg.cache.walk_ttl_d * 86400

    for i, row in enumerate(listings, 1):
        _check(cancel)
        progress(
            stage="routing",
            message=f"{row['city_part'] or ''} {row['title'] or row['id']}".strip(),
            done=i - 1,
            total=len(listings),
        )
        lat, lon = row["lat"], row["lon"]

        key = routing.route_key(lat, lon, cfg.office, routing.TRANSIT, arrive_key)
        if not opts.force_refresh and store.route(key, transit_ttl) is not None:
            result.routes_cached += 1
        else:
            try:
                res = router.transit(lat, lon, cfg.office, arrive)
            except BudgetExceeded:
                raise
            except Exception as exc:
                result.warnings.append(f"transit {row['id']}: {type(exc).__name__}: {exc}")
                res = None
            if res:
                store.put_route(key, row["id"], routing.TRANSIT, arrive_key, res)
                result.routes_computed += 1

        key = routing.route_key(lat, lon, cfg.office, routing.WALK, arrive_key)
        if not opts.force_refresh and store.route(key, walk_ttl) is not None:
            result.routes_cached += 1
        else:
            km = routing.haversine_km(lat, lon, cfg.office.lat, cfg.office.lon)
            if km > cfg.commute.walk_max_km:
                store.put_route(
                    key, row["id"], routing.WALK,
                    routing.stamp_for(routing.WALK, arrive_key),
                    {"status": "too_far", "summary": f"{km:.1f} km direct"},
                )
                continue
            try:
                res = router.walk(lat, lon, cfg.office, arrive)
            except BudgetExceeded:
                raise
            except Exception as exc:
                result.warnings.append(f"walk {row['id']}: {type(exc).__name__}: {exc}")
                res = None
            if res:
                store.put_route(
                    key, row["id"], routing.WALK,
                    routing.stamp_for(routing.WALK, arrive_key), res,
                )
                result.routes_computed += 1
