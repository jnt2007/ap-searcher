import pytest

from ap_searcher import sreality
from ap_searcher.config import Profile


def test_build_search_url_encodes_filters():
    profile = Profile(
        name="p", deal="pronajem", kind="byty", locality="praha",
        price_from=15000, price_to=30000, area_from=40, dispositions=["2+kk", "3+kk"],
    )
    url = sreality.build_search_url(profile, page=2)
    assert url.startswith("https://www.sreality.cz/hledani/pronajem/byty/praha?")
    assert "velikost=2%2Bkk%2C3%2Bkk" in url
    assert "cena-od=15000" in url and "cena-do=30000" in url
    assert "plocha-od=40" in url and "strana=2" in url


def test_build_search_url_omits_empty_filters():
    url = sreality.build_search_url(Profile(name="p"), page=1)
    assert url == "https://www.sreality.cz/hledani/pronajem/byty/praha"


def test_extract_search_reads_results_and_total(search_html):
    page = sreality.extract_search(search_html)
    assert page.total > 0
    assert len(page.results) >= 10
    assert page.query_key["categoryMainCb"] == [1]


def test_check_filters_accepts_matching_request(search_html):
    page = sreality.extract_search(search_html)
    key = page.query_key
    profile = Profile(
        name="p",
        price_from=key.get("priceFrom"),
        price_to=key.get("priceTo"),
        area_from=key.get("usableAreaFrom"),
        dispositions=["2+kk", "3+kk"],
    )
    assert sreality.check_filters(profile, key) == []


def test_check_filters_reports_ignored_filter():
    profile = Profile(name="p", price_to=30000, dispositions=["2+kk"])
    key = {"categoryMainCb": [1], "categoryTypeCb": [2], "localityRegionId": [10],
           "categorySubCb": [4]}
    problems = sreality.check_filters(profile, key)
    assert any("price_to" in p for p in problems)


def test_check_filters_flags_unknown_locality():
    profile = Profile(name="p", locality="atlantis")
    problems = sreality.check_filters(profile, {"categoryMainCb": [1], "categoryTypeCb": [2]})
    assert any("locality" in p for p in problems)


def test_listing_from_result_flattens(sample_listing):
    listing = sreality.listing_from_result(sample_listing)
    assert listing["id"] == sample_listing["id"]
    assert listing["url"].startswith("https://www.sreality.cz/detail/")
    assert str(listing["id"]) in listing["url"]
    assert listing["lat"] and listing["lon"]
    assert listing["area_m2"] and listing["area_m2"] > 10
    assert listing["image_url"] is None or listing["image_url"].startswith("https://")


def test_area_parsed_from_title():
    assert sreality._area_from_name("Pronájem bytu 3+kk 85 m²") == 85.0
    assert sreality._area_from_name("Pronájem bytu") is None


def test_extract_detail_returns_description_and_params(detail_html):
    detail = sreality.extract_detail(detail_html)
    assert len(detail["description"]) > 50
    assert isinstance(detail["params"], dict)
    assert detail["area_m2"]


def test_parse_error_on_foreign_page():
    with pytest.raises(sreality.ParseError):
        sreality.extract_search("<html><body>nothing here</body></html>")


def test_image_url_gets_the_cdn_transform(sample_listing):
    listing = sreality.listing_from_result(sample_listing)
    if listing["image_url"]:
        assert listing["image_url"].startswith("https://")
        assert sreality.IMAGE_TRANSFORM in listing["image_url"]


BBOX = dict(lat_min=50.07157419393176, lat_max=50.12140357700609,
            lon_min=14.42745066703636, lon_max=14.480236540449447)


def test_build_search_url_includes_bounding_box():
    url = sreality.build_search_url(Profile(name="p", **BBOX), page=1)
    for key, value in (("lat-min", BBOX["lat_min"]), ("lat-max", BBOX["lat_max"]),
                       ("lon-min", BBOX["lon_min"]), ("lon-max", BBOX["lon_max"])):
        assert f"{key}={value}" in url


def test_build_search_url_omits_bounding_box_when_unset():
    url = sreality.build_search_url(Profile(name="p"), page=1)
    assert "lat-min" not in url and "lon-max" not in url


def test_check_filters_accepts_echoed_bounding_box():
    profile = Profile(name="p", **BBOX)
    key = {"categoryMainCb": [1], "categoryTypeCb": [2], "localityRegionId": [10],
           "latMin": BBOX["lat_min"], "latMax": BBOX["lat_max"],
           "lonMin": BBOX["lon_min"], "lonMax": BBOX["lon_max"]}
    assert sreality.check_filters(profile, key) == []


def test_check_filters_reports_dropped_bounding_box():
    profile = Profile(name="p", **BBOX)
    key = {"categoryMainCb": [1], "categoryTypeCb": [2], "localityRegionId": [10]}
    problems = sreality.check_filters(profile, key)
    assert len(problems) == 4
    assert any("lat_min" in p for p in problems)


def test_check_filters_reports_shifted_bounding_box():
    profile = Profile(name="p", **BBOX)
    key = {"categoryMainCb": [1], "categoryTypeCb": [2], "localityRegionId": [10],
           "latMin": 49.0, "latMax": BBOX["lat_max"],
           "lonMin": BBOX["lon_min"], "lonMax": BBOX["lon_max"]}
    assert [p for p in sreality.check_filters(profile, key) if "lat_min" in p]


def test_extract_detail_returns_publication_dates(detail_html):
    detail = sreality.extract_detail(detail_html)
    assert detail["published_on"] and detail["published_on"][:2] == "20"
    assert len(detail["published_on"]) == 10  # YYYY-MM-DD
    assert detail["edited_on"] is None or len(detail["edited_on"]) == 10


def test_missing_dates_become_none():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"dehydratedState":{"queries":[{"queryHash":"[\\"estate\\",{}]",'
        '"state":{"data":{"description":"x","params":{"since":"","edited":null}}}}]}}}}'
        "</script>"
    )
    detail = sreality.extract_detail(html)
    assert detail["published_on"] is None and detail["edited_on"] is None


def test_furnished_state_mapping():
    assert sreality.furnished_state({"furnished": {"value": 1, "name": "Ano"}}) == "yes"
    assert sreality.furnished_state({"furnished": {"value": 2, "name": "Ne"}}) == "no"
    assert sreality.furnished_state({"furnished": {"value": 3, "name": "Částečně"}}) == "partly"


def test_blank_furnished_is_unknown_not_no():
    # value 0 is sreality's "- vyber vybavení" placeholder, i.e. the advertiser said nothing
    assert sreality.furnished_state({"furnished": {"value": 0, "name": "- vyber vybavení"}}) == "unknown"
    assert sreality.furnished_state({"furnished": None}) == "unknown"
    assert sreality.furnished_state({}) == "unknown"


def test_extract_detail_reports_furnished(detail_html):
    assert sreality.extract_detail(detail_html)["furnished"] in ("yes", "no", "partly", "unknown")


def test_furnished_filter_uses_codebook_names_in_the_url():
    profile = Profile(name="p", locality="praha-9", furnished=["partly", "no"])
    url = sreality.build_search_url(profile, 1)
    assert "vybaveni=castecne%2Cnevybaveny" in url


def test_furnished_filter_is_asserted_against_the_echo():
    """The site answers with the numeric enum, not the codebook names we sent."""
    profile = Profile(name="p", locality="praha-9", furnished=["partly", "no"])
    key = {
        "categoryMainCb": [1], "categoryTypeCb": [2],
        "localityDistrictId": [5009], "furnished": [2, 3],
    }
    assert sreality.check_filters(profile, key) == []

    key_without = dict(key, furnished=None)
    assert sreality.check_filters(profile, key_without) == [
        "furnished: asked [2, 3], site applied []"
    ]

    key_wrong = dict(key, furnished=[1])
    assert "furnished: asked [2, 3], site applied [1]" in sreality.check_filters(profile, key_wrong)


def test_no_furnished_filter_means_no_query_parameter():
    profile = Profile(name="p", locality="praha-9")
    assert "vybaveni" not in sreality.build_search_url(profile, 1)
    assert sreality.check_filters(
        profile, {"categoryMainCb": [1], "categoryTypeCb": [2], "localityDistrictId": [5009]}
    ) == []
