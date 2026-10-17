"""Pipeline tests with a stubbed fetcher - no network, no routing service."""

import threading

import pytest

from ap_searcher import config as config_mod
from ap_searcher import pipeline
from ap_searcher.http import Counters
from ap_searcher.pipeline import RunOptions


class StubFetcher:
    """Serves fixture HTML and counts calls, mimicking http.Fetcher."""

    def __init__(self, search_html, detail_html, **kwargs):
        self.search_html = search_html
        self.detail_html = detail_html
        self.counters = Counters()
        self.urls = []

    def get(self, url, max_age_s, force=False):
        self.urls.append(url)
        self.counters.requests += 1
        return self.detail_html if "/detail/" in url else self.search_html


@pytest.fixture(autouse=True)
def profile_matching_the_fixture(cfg):
    """Pin the profiles to the filters the saved search page was captured with.

    check_filters would (correctly) flag anything else as a mismatch - the fixture
    page echoes priceFrom 15000 / priceTo 30000 / usableAreaFrom 40 / no bounding
    box - and the shipped config.yaml is free to drift from that.
    """
    fixture_profile = config_mod.Profile(
        name="fixture",
        locality="praha",
        price_from=15000,
        price_to=30000,
        area_from=40,
        dispositions=["2+kk", "3+kk"],
        max_pages=5,
    )
    cfg.profiles = [fixture_profile]
    return cfg


@pytest.fixture
def stub_fetchers(monkeypatch, search_html, detail_html):
    made = []

    def factory(**kwargs):
        fetcher = StubFetcher(search_html, detail_html, **kwargs)
        made.append(fetcher)
        return fetcher

    monkeypatch.setattr(pipeline, "Fetcher", factory)
    return made


def test_search_and_details_populate_the_store(cfg, store, stub_fetchers):
    result = pipeline.run(
        cfg, RunOptions(max_pages=1, do_routing=False), store
    )
    assert "search" in result.stages and "details" in result.stages
    assert result.new_listings, "expected new listings on first run"
    assert result.details_fetched == len(store.listings())
    assert result.warnings == []

    row = store.listings()[0]
    assert row["url"].startswith("https://www.sreality.cz/detail/")
    assert store.detail(row["id"]) is not None
    assert store.amenities_map()[row["id"]]["parking"]["state"] in ("yes", "maybe", "unknown")


def test_second_run_reports_nothing_new(cfg, store, stub_fetchers):
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    second = pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    assert second.new_listings == []
    assert second.details_fetched == 0
    assert second.details_cached == len(store.listings())


def test_price_change_is_detected(cfg, store, stub_fetchers):
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    row = store.listings()[0]
    store.upsert_listing(
        {"id": row["id"], "title": row["title"], "price_czk": (row["price_czk"] or 0) + 1000},
        "praha-rent-2kk-3kk",
    )
    result = pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    assert any(c["id"] == row["id"] for c in result.price_changes)


def test_cancel_stops_the_run(cfg, store, stub_fetchers):
    cancel = threading.Event()
    cancel.set()
    result = pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store, cancel=cancel)
    assert "run cancelled" in result.warnings
    assert store.listings() == []


def test_filter_mismatch_surfaces_as_warning(cfg, store, stub_fetchers):
    cfg.profiles[0].price_to = 1  # the fixture page was fetched with a different filter
    result = pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    assert any("filter mismatch" in w for w in result.warnings)


def test_routing_skips_far_listings_without_calling_the_router(cfg, store, stub_fetchers, monkeypatch):
    calls = []

    class NoWalkRouter:
        def __init__(self, *args, **kwargs):
            pass

        def transit(self, lat, lon, office, arrive):
            calls.append(("transit", lat, lon))
            return {"status": "ok", "duration_s": 1200, "transfers": 1, "summary": "metro A", "legs": []}

        def walk(self, lat, lon, office, arrive):
            calls.append(("walk", lat, lon))
            return {"status": "ok", "duration_s": 3000, "summary": "walk", "legs": []}

    monkeypatch.setattr(pipeline.routing, "Router", NoWalkRouter)
    cfg.commute.walk_max_km = 0.1  # everything is "too far" to walk
    pipeline.run(cfg, RunOptions(max_pages=1), store)

    assert calls and all(mode == "transit" for mode, *_ in calls)
    walk_rows = [r for r in store.routes_by_key().values() if r["mode"] == "walk"]
    assert walk_rows and all(r["status"] == "too_far" for r in walk_rows)


def test_details_persist_publication_dates(cfg, store, stub_fetchers):
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    rows = store.listings()
    assert rows and all(r["published_on"] for r in rows)


def test_dates_are_backfilled_without_refetching(cfg, store, stub_fetchers):
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    store.conn.execute("UPDATE listings SET published_on = NULL, edited_on = NULL")
    store.conn.commit()

    fetchers_before = len(stub_fetchers)
    pipeline.run(cfg, RunOptions(do_search=False, do_details=True, do_routing=False), store)

    new_fetchers = stub_fetchers[fetchers_before:]
    assert new_fetchers, "the second run should have built its own fetchers"
    assert all(f.counters.requests == 0 for f in new_fetchers), "dates must come from the cache"
    assert all(r["published_on"] for r in store.listings())


def test_walk_routes_survive_the_week_rolling_over(cfg, store, stub_fetchers, monkeypatch):
    """A new arrival date must invalidate transit routes but reuse walking ones."""
    import datetime as dt

    calls = []

    class CountingRouter:
        def __init__(self, *args, **kwargs):
            pass

        def transit(self, lat, lon, office, arrive):
            calls.append("transit")
            return {"status": "ok", "duration_s": 1200, "transfers": 1, "summary": "metro A", "legs": []}

        def walk(self, lat, lon, office, arrive):
            calls.append("walk")
            return {"status": "ok", "duration_s": 3000, "summary": "walk", "legs": []}

    monkeypatch.setattr(pipeline.routing, "Router", CountingRouter)
    cfg.commute.walk_max_km = 500  # keep every listing inside the walking radius

    pipeline.run(cfg, RunOptions(max_pages=1), store)
    first_walks = calls.count("walk")
    assert first_walks > 0

    # pretend a week has passed: the arrival basis moves to the next Tuesday
    real_next = cfg.commute.next_arrival
    monkeypatch.setattr(
        cfg.commute, "next_arrival", lambda *a, **k: real_next() + dt.timedelta(days=7)
    )
    calls.clear()
    pipeline.run(cfg, RunOptions(do_search=False, do_details=False, do_routing=True), store)

    assert calls.count("transit") > 0, "transit must be recomputed for the new arrival time"
    assert calls.count("walk") == 0, "walking must be reused across weeks"


def test_details_persist_furnished_state(cfg, store, stub_fetchers):
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    rows = store.listings()
    assert rows and all(r["furnished"] in ("yes", "no", "partly", "unknown") for r in rows)


def test_furnished_is_backfilled_without_refetching(cfg, store, stub_fetchers):
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)
    store.conn.execute("UPDATE listings SET furnished = NULL")
    store.conn.commit()

    fetchers_before = len(stub_fetchers)
    pipeline.run(cfg, RunOptions(do_search=False, do_details=True, do_routing=False), store)
    new_fetchers = stub_fetchers[fetchers_before:]
    assert all(f.counters.requests == 0 for f in new_fetchers)
    assert all(r["furnished"] is not None for r in store.listings())


def test_neighborhood_filter_keeps_only_matching_city_parts(cfg, store, stub_fetchers):
    """sreality has no ward filter, so the profile is narrowed on our side."""
    cfg.profiles[0].neighborhoods = ["Nusle"]
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)

    kept = store.listings()
    assert kept, "the fixture page contains Nusle listings"
    assert {row["city_part"] for row in kept} == {"Nusle"}


def test_neighborhood_names_match_regardless_of_diacritics(cfg, store, stub_fetchers):
    cfg.profiles[0].neighborhoods = ["vysehrad", "Nové Město"]
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False), store)

    assert {row["city_part"] for row in store.listings()} == {"Nové Město"}


def test_run_options_accept_one_or_several_profile_names():
    assert RunOptions().profile_names() == []
    assert RunOptions(profile="alpha").profile_names() == ["alpha"]
    assert RunOptions(profile="alpha,beta").profile_names() == ["alpha", "beta"]
    assert RunOptions(profile=" alpha , beta ,,").profile_names() == ["alpha", "beta"]
    assert RunOptions(profile=["alpha", "beta"]).profile_names() == ["alpha", "beta"]


def test_run_sweeps_only_the_selected_profiles(cfg, store, stub_fetchers, monkeypatch):
    cfg.profiles = [
        config_mod.Profile(name=name, price_from=15000, price_to=30000, area_from=40,
                           dispositions=["2+kk", "3+kk"])
        for name in ("alpha", "beta", "gamma")
    ]
    swept = []
    original = pipeline._search

    def spy(cfg_, opts, store_, profiles, *args):
        swept.extend(p.name for p in profiles)
        return original(cfg_, opts, store_, profiles, *args)

    monkeypatch.setattr(pipeline, "_search", spy)
    pipeline.run(cfg, RunOptions(profile="alpha,gamma", max_pages=1,
                                 do_details=False, do_routing=False), store)
    assert swept == ["alpha", "gamma"]


def test_a_profile_sweeps_every_area_with_its_own_neighbourhoods(cfg, store, stub_fetchers):
    """Praha 9 whole + only Holešovice of Praha 7, in one profile."""
    cfg.profiles[0].areas = [
        config_mod.Area(locality="praha-9", neighborhoods=["Nusle"]),
        config_mod.Area(locality="praha-7", neighborhoods=["Vinohrady"]),
    ]
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False, do_details=False), store)

    urls = stub_fetchers[0].urls
    assert any("/byty/praha-9?" in u for u in urls) and any("/byty/praha-7?" in u for u in urls)
    # Each area contributes only its own city part, and the two sets add up.
    assert {row["city_part"] for row in store.listings()} == {"Nusle", "Vinohrady"}


def test_an_area_without_neighbourhoods_keeps_everything(cfg, store, stub_fetchers):
    cfg.profiles[0].areas = [config_mod.Area(locality="praha-9")]
    pipeline.run(cfg, RunOptions(max_pages=1, do_routing=False, do_details=False), store)
    assert len({row["city_part"] for row in store.listings()}) > 1
