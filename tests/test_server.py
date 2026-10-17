import json

from ap_searcher import amenities as amen
from ap_searcher import sreality
from ap_searcher.jobs import JobManager
from ap_searcher.server import Api, build_view


def api(cfg):
    return Api(cfg, JobManager(cfg))


def seed(store, search_html, detail_html):
    page = sreality.extract_search(search_html)
    raw = page.results[0]
    listing = sreality.listing_from_result(raw)
    store.upsert_listing(listing, "praha-rent-2kk-3kk")
    detail = sreality.extract_detail(detail_html)
    store.put_detail(listing["id"], detail["description"], detail["params"], detail["transport"])
    store.put_amenities(listing["id"], amen.detect(detail["description"], detail["params"]))
    return listing


def test_index_renders_office_and_profiles(cfg):
    html = api(cfg).index_html()
    assert cfg.office.name in html
    assert cfg.profiles[0].name in html
    assert "/static/app.js" in html


def test_listings_endpoint_shape(cfg, store, search_html, detail_html):
    listing = seed(store, search_html, detail_html)
    status, payload = api(cfg).handle("GET", "/api/listings", {}, {})
    assert status == 200
    assert payload["office"]["lat"] == cfg.office.lat
    row = next(l for l in payload["listings"] if l["id"] == listing["id"])
    assert row["is_new"] is True
    assert set(row["amenities"]) == set(amen.KEYS)
    assert row["transit"] is None  # no routing has run yet


def test_hidden_listings_are_filtered_unless_requested(cfg, store, search_html, detail_html):
    listing = seed(store, search_html, detail_html)
    store.set_mark(listing["id"], "hide")
    _, visible = api(cfg).handle("GET", "/api/listings", {}, {})
    assert visible["listings"] == []
    _, all_rows = api(cfg).handle("GET", "/api/listings", {"include_hidden": "1"}, {})
    assert all_rows["listings"][0]["mark"] == "hide"


def test_mark_endpoint_persists(cfg, store, search_html, detail_html):
    listing = seed(store, search_html, detail_html)
    status, _ = api(cfg).handle("POST", "/api/mark", {}, {"id": listing["id"], "state": "star"})
    assert status == 200
    assert store.marks_map()[listing["id"]]["state"] == "star"


def test_mark_endpoint_validates_state(cfg):
    status, payload = api(cfg).handle("POST", "/api/mark", {}, {"id": 1, "state": "burn"})
    assert status == 400 and "error" in payload


def test_second_run_conflicts_with_the_first(cfg):
    instance = api(cfg)

    def busy(*args, **kwargs):
        raise RuntimeError("a job is already running")

    instance.jobs.start = busy
    status, payload = instance.handle("POST", "/api/run", {}, {"search": True})
    assert status == 409 and "already running" in payload["error"]


def test_weights_endpoint_updates_config_file(cfg):
    status, payload = api(cfg).handle("POST", "/api/weights", {}, {"weights": {"commute": 1.7}})
    assert status == 200 and payload["weights"]["commute"] == 1.7
    assert "1.7" in cfg.path.read_text()


def test_cache_endpoints(cfg, store):
    store.http_put("http://x", "sreality", 200, "body")
    status, stats = api(cfg).handle("GET", "/api/cache", {}, {})
    assert status == 200 and stats["http_cache"] == 1
    status, purged = api(cfg).handle("DELETE", "/api/cache", {"kind": "http"}, {})
    assert status == 200 and purged["removed"] == 1


def test_unknown_route_is_404(cfg):
    status, payload = api(cfg).handle("GET", "/api/nope", {}, {})
    assert status == 404 and "error" in payload


def test_build_view_matches_routes_by_coordinates(cfg, store, search_html, detail_html):
    from ap_searcher import routing

    listing = seed(store, search_html, detail_html)
    arrive = cfg.commute.next_arrival().isoformat()
    key = routing.route_key(listing["lat"], listing["lon"], cfg.office, routing.TRANSIT, arrive)
    store.put_route(key, 999, routing.TRANSIT, arrive,
                    {"duration_s": 1500, "transfers": 1, "summary": "metro A", "legs": []})
    # stored against a different listing id: the same building must still resolve
    view = build_view(cfg, store, None, True)
    row = next(l for l in view["listings"] if l["id"] == listing["id"])
    assert row["transit"]["minutes"] == 25
    assert row["transit"]["summary"] == "metro A"


def test_listings_expose_publication_dates(cfg, store, search_html, detail_html):
    listing = seed(store, search_html, detail_html)
    store.set_dates(listing["id"], "2026-09-03", "2026-09-19")
    _, payload = api(cfg).handle("GET", "/api/listings", {}, {})
    row = next(l for l in payload["listings"] if l["id"] == listing["id"])
    assert row["published_on"] == "2026-09-03"
    assert row["edited_on"] == "2026-09-19"


def test_page_defaults_to_the_configured_sweep_size(cfg):
    for profile in cfg.profiles:
        profile.max_pages = 5
    html = api(cfg).index_html()
    assert 'id="max-pages"' in html and 'value="5"' in html


def test_routing_budget_defaults_to_the_configured_value(cfg):
    cfg.routing.max_requests_per_run = 450
    html = api(cfg).index_html()
    assert 'id="budget"' in html and 'value="450"' in html


def test_walk_routes_resolve_regardless_of_arrival_week(cfg, store, search_html, detail_html):
    from ap_searcher import routing

    listing = seed(store, search_html, detail_html)
    stale_arrival = "2026-01-01T09:00:00+01:00"  # written for a different week
    key = routing.route_key(listing["lat"], listing["lon"], cfg.office, routing.WALK, stale_arrival)
    store.put_route(key, listing["id"], routing.WALK, routing.TIMELESS,
                    {"duration_s": 1800, "summary": "walk 30′", "legs": []})

    view = build_view(cfg, store, None, True)
    row = next(l for l in view["listings"] if l["id"] == listing["id"])
    assert row["walk"]["minutes"] == 30


def test_listings_expose_furnished_state(cfg, store, search_html, detail_html):
    listing = seed(store, search_html, detail_html)
    store.set_furnished(listing["id"], "partly")
    _, payload = api(cfg).handle("GET", "/api/listings", {}, {})
    row = next(l for l in payload["listings"] if l["id"] == listing["id"])
    assert row["furnished"] == "partly"


def test_furnished_filter_controls_are_rendered(cfg):
    html = api(cfg).index_html()
    assert 'id="f-furnished"' in html and 'id="f-partly"' in html


def test_listings_endpoint_accepts_several_profiles(cfg, store, search_html, detail_html):
    """The `profile` parameter takes a comma-separated selection; empty means all."""
    page = sreality.extract_search(search_html)
    first = sreality.listing_from_result(page.results[0])
    second = sreality.listing_from_result(page.results[1])
    third = sreality.listing_from_result(page.results[2])
    store.upsert_listing(first, "alpha")
    store.upsert_listing(second, "beta")
    store.upsert_listing(third, "gamma")

    def ids(profile):
        _, payload = api(cfg).handle("GET", "/api/listings", {"profile": profile} if profile else {}, {})
        return {row["id"] for row in payload["listings"]}, payload["selected_profiles"]

    assert ids("alpha") == ({first["id"]}, ["alpha"])
    assert ids("alpha,beta") == ({first["id"], second["id"]}, ["alpha", "beta"])
    assert ids(" alpha , beta ,,") == ({first["id"], second["id"]}, ["alpha", "beta"])
    assert ids(None) == ({first["id"], second["id"], third["id"]}, [])


def test_a_listing_in_two_selected_profiles_appears_once(cfg, store, search_html, detail_html):
    page = sreality.extract_search(search_html)
    listing = sreality.listing_from_result(page.results[0])
    store.upsert_listing(listing, "alpha")
    store.upsert_listing(listing, "beta")

    _, payload = api(cfg).handle("GET", "/api/listings", {"profile": "alpha,beta"}, {})
    assert [row["id"] for row in payload["listings"]] == [listing["id"]]
    assert sorted(payload["listings"][0]["profiles"]) == ["alpha", "beta"]
