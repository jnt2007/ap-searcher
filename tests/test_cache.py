import time

from ap_searcher.cache import Store


def test_new_listing_then_price_change(store):
    flags = store.upsert_listing({"id": 1, "title": "t", "price_czk": 20000}, "p")
    assert flags["is_new"] and flags["price_delta"] is None

    flags = store.upsert_listing({"id": 1, "title": "t", "price_czk": 20000}, "p")
    assert not flags["is_new"] and flags["price_delta"] is None

    flags = store.upsert_listing({"id": 1, "title": "t", "price_czk": 18000}, "p")
    assert flags["price_delta"] == -2000
    assert [r["price_czk"] for r in store.price_history(1)] == [20000, 18000]


def test_listing_can_belong_to_several_profiles(store):
    store.upsert_listing({"id": 7, "title": "t"}, "rent")
    store.upsert_listing({"id": 7, "title": "t"}, "buy")
    assert sorted(store.profiles_of(7)) == ["buy", "rent"]
    assert len(store.listings("rent")) == 1
    assert len(store.listings()) == 1


def test_mark_removed_only_touches_unseen(store):
    store.upsert_listing({"id": 1}, "p")
    store.upsert_listing({"id": 2}, "p")
    store.mark_removed("p", [1], time.time())
    rows = {r["id"]: r["removed_at"] for r in store.listings("p")}
    assert rows[1] is None and rows[2] is not None


def test_reappearing_listing_clears_removed_flag(store):
    store.upsert_listing({"id": 1}, "p")
    store.mark_removed("p", [], time.time())
    store.upsert_listing({"id": 1}, "p")
    assert store.listings("p")[0]["removed_at"] is None


def test_http_cache_respects_ttl(store):
    store.http_put("http://x", "sreality", 200, "body")
    assert store.http_get("http://x", max_age_s=60) == "body"
    assert store.http_get("http://x", max_age_s=0) is None
    assert store.http_get("http://missing", max_age_s=60) is None


def test_routes_cached_by_key_not_listing(store):
    store.put_route("k1", 1, "transit", "2026-09-22T09:00:00", {"duration_s": 600, "transfers": 1})
    assert store.route("k1", max_age_s=60)["duration_s"] == 600
    assert store.route("k1", max_age_s=0) is None
    assert store.routes_by_key()["k1"]["mode"] == "transit"


def test_marks_roundtrip_and_clear(store):
    store.set_mark(3, "star", note="nice terrace")
    assert store.marks_map()[3] == {"state": "star", "note": "nice terrace", "viewed": False}
    store.set_mark(3, "hide")
    assert store.marks_map()[3]["state"] == "hide"
    store.set_mark(3, "clear")
    assert 3 not in store.marks_map()


def test_purge_http_cache(store):
    store.http_put("http://x", "sreality", 200, "body")
    assert store.purge("http") == 1
    assert store.stats()["http_cache"] == 0


def test_schema_is_reopenable(cfg):
    with Store(cfg.cache.path) as first:
        first.upsert_listing({"id": 1}, "p")
    with Store(cfg.cache.path) as second:
        assert len(second.listings()) == 1


def test_http_bodies_are_stored_compressed(store):
    body = "<html>" + "x" * 200_000 + "</html>"
    store.http_put("http://big", "sreality", 200, body)
    assert store.http_get("http://big", max_age_s=60) == body
    stored = store.conn.execute("SELECT body FROM http_cache WHERE url='http://big'").fetchone()
    assert len(stored["body"]) < len(body) / 10


def test_plaintext_rows_from_older_versions_still_read(store):
    store.conn.execute(
        "INSERT INTO http_cache (url, kind, fetched_at, status, body) VALUES (?,?,?,?,?)",
        ("http://legacy", "sreality", __import__("time").time(), 200, "<html>old</html>"),
    )
    store.conn.commit()
    assert store.http_get("http://legacy", max_age_s=60) == "<html>old</html>"


def test_migration_adds_date_columns_to_an_old_database(cfg):
    from ap_searcher import cache as cache_mod

    with Store(cfg.cache.path) as s:
        s.upsert_listing({"id": 1, "title": "t"}, "p")
        # simulate a database created before the columns existed
        s.conn.execute("ALTER TABLE listings DROP COLUMN published_on")
        s.conn.execute("ALTER TABLE listings DROP COLUMN edited_on")
        s.conn.commit()
        columns = {r["name"] for r in s.conn.execute("PRAGMA table_info(listings)")}
        assert "published_on" not in columns

    with Store(cfg.cache.path) as reopened:  # connect() runs the migration
        columns = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(listings)")}
        assert {"published_on", "edited_on"} <= columns
        assert reopened.listings()[0]["published_on"] is None


def test_migration_is_idempotent(store):
    from ap_searcher import cache as cache_mod

    assert cache_mod.migrate(store.conn) == []


def test_set_dates(store):
    store.upsert_listing({"id": 1, "title": "t"}, "p")
    store.set_dates(1, "2026-09-03", "2026-09-19")
    row = store.listings()[0]
    assert row["published_on"] == "2026-09-03" and row["edited_on"] == "2026-09-19"


def test_legacy_walk_keys_are_rekeyed_on_connect(cfg):
    from ap_searcher import cache as cache_mod

    old_key = "50.05000,14.50000|50.09411,14.44655|walk|2026-09-22T09:00:00+02:00"
    with Store(cfg.cache.path) as s:
        s.put_route(old_key, 1, "walk", "2026-09-22T09:00:00+02:00", {"duration_s": 900})

    with Store(cfg.cache.path) as reopened:  # connect() migrates
        keys = list(reopened.routes_by_key())
        assert keys == ["50.05000,14.50000|50.09411,14.44655|walk|any"]
        row = reopened.routes_by_key()[keys[0]]
        assert row["arrive_by"] == "any" and row["duration_s"] == 900
        assert cache_mod.migrate(reopened.conn) == []  # idempotent


def test_rekeying_collapses_duplicate_weeks_keeping_the_newest(cfg):
    import time as _time

    base = "50.05000,14.50000|50.09411,14.44655|walk"
    with Store(cfg.cache.path) as s:
        s.put_route(f"{base}|2026-09-15T09:00:00", 1, "walk", "2026-09-15T09:00:00", {"duration_s": 111})
        s.conn.execute("UPDATE routes SET fetched_at = ? WHERE key LIKE '%2026-09-15%'",
                       (_time.time() - 86400,))
        s.put_route(f"{base}|2026-09-22T09:00:00", 1, "walk", "2026-09-22T09:00:00", {"duration_s": 222})
        s.conn.commit()

    with Store(cfg.cache.path) as reopened:
        rows = reopened.routes_by_key()
        assert list(rows) == [f"{base}|any"]
        assert rows[f"{base}|any"]["duration_s"] == 222  # the most recent one survived


def test_transit_rows_are_left_alone_by_rekeying(cfg):
    transit_key = "50.05000,14.50000|50.09411,14.44655|transit|2026-09-22T09:00:00"
    with Store(cfg.cache.path) as s:
        s.put_route(transit_key, 1, "transit", "2026-09-22T09:00:00", {"duration_s": 1500})
    with Store(cfg.cache.path) as reopened:
        assert transit_key in reopened.routes_by_key()


def test_viewed_is_independent_of_the_shortlist_choice(store):
    store.set_mark(4, "star")
    store.set_viewed(4, True)
    assert store.marks_map()[4] == {"state": "star", "note": None, "viewed": True}

    # Clearing star must not clear viewed - they are separate flags on one row.
    store.set_mark(4, "clear")
    assert store.marks_map()[4] == {"state": None, "note": None, "viewed": True}

    store.set_mark(4, "hide")
    assert store.marks_map()[4] == {"state": "hide", "note": None, "viewed": True}


def test_clearing_both_flags_drops_the_row(store):
    store.set_viewed(5, True)
    assert store.marks_map()[5]["viewed"] is True
    store.set_viewed(5, False)
    assert 5 not in store.marks_map()

    store.set_mark(6, "star")
    store.set_mark(6, "clear")
    assert 6 not in store.marks_map()


def test_viewed_survives_a_reopen(store, cfg):
    store.set_viewed(7, True)
    with Store(cfg.cache.path) as reopened:
        assert reopened.marks_map()[7]["viewed"] is True
