import datetime as dt

from ap_searcher import routing
from ap_searcher.config import Office


OFFICE = Office(name="office", lat=50.0941093, lon=14.4465543)


def test_haversine_matches_known_distance():
    km = routing.haversine_km(50.0755, 14.4378, 50.0941093, 14.4465543)
    assert 2.0 < km < 2.4


def test_route_key_rounds_nearby_coordinates_together():
    a = routing.route_key(50.053012588, 14.502881169, OFFICE, "transit", "2026-09-22T09:00")
    b = routing.route_key(50.053012999, 14.502881999, OFFICE, "transit", "2026-09-22T09:00")
    assert a == b


def test_route_key_separates_modes():
    base = dict(lat=50.05, lon=14.50, office=OFFICE)
    assert routing.route_key(mode="walk", arrive_by="t", **base) != routing.route_key(
        mode="transit", arrive_by="t", **base
    )


def test_transit_keys_are_tied_to_the_arrival_moment():
    base = dict(lat=50.05, lon=14.50, office=OFFICE, mode="transit")
    assert routing.route_key(arrive_by="2026-09-22T09:00", **base) != routing.route_key(
        arrive_by="2026-09-29T09:00", **base
    )


def test_walk_keys_ignore_the_arrival_moment():
    """Walking time cannot change with the timetable, so it must not expire weekly."""
    base = dict(lat=50.05, lon=14.50, office=OFFICE, mode="walk")
    week1 = routing.route_key(arrive_by="2026-09-22T09:00", **base)
    week2 = routing.route_key(arrive_by="2026-09-29T09:00", **base)
    assert week1 == week2
    assert week1.endswith("|any")


def test_stamp_for():
    assert routing.stamp_for("transit", "2026-09-22T09:00") == "2026-09-22T09:00"
    assert routing.stamp_for("walk", "2026-09-22T09:00") == routing.TIMELESS


def test_summarise_builds_readable_line():
    itinerary = {
        "duration": 1860,
        "transfers": 1,
        "legs": [
            {"mode": "WALK", "duration": 60, "from": {"name": "START"}, "to": {"name": "Stop"}},
            {"mode": "BUS", "routeShortName": "138", "duration": 480,
             "from": {"name": "Stop"}, "to": {"name": "Skalka"}},
            {"mode": "SUBWAY", "routeShortName": "A", "duration": 600,
             "from": {"name": "Skalka"}, "to": {"name": "Muzeum"}},
            {"mode": "WALK", "duration": 540, "from": {"name": "Muzeum"}, "to": {"name": "END"}},
        ],
    }
    result = routing._summarise(itinerary)
    assert result["status"] == "ok"
    assert result["duration_s"] == 1860
    assert result["summary"] == "walk 1′ · bus 138 · metro A · walk 9′"
    assert len(result["legs"]) == 4


def test_summarise_skips_sub_minute_walks():
    itinerary = {"duration": 300, "legs": [
        {"mode": "WALK", "duration": 20, "from": {}, "to": {}},
        {"mode": "TRAM", "routeShortName": "3", "duration": 280, "from": {}, "to": {}},
    ]}
    assert routing._summarise(itinerary)["summary"] == "tram 3"


def test_iso_utc_converts_from_prague():
    prague = dt.datetime(2026, 9, 22, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert routing._iso_utc(prague) == "2026-09-22T07:00:00Z"
