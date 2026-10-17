import datetime as dt
import pathlib

import pytest
import yaml

from ap_searcher import config as config_mod
from ap_searcher.config import Commute, PRAGUE

ROOT = pathlib.Path(__file__).resolve().parents[1]

from conftest import ROOT_CONFIG  # noqa: E402


def test_next_arrival_picks_the_configured_weekday():
    commute = Commute(arrive_by="09:00", weekday="tuesday")
    now = dt.datetime(2026, 9, 19, 15, 0, tzinfo=PRAGUE)  # a Saturday
    arrival = commute.next_arrival(now)
    assert arrival.weekday() == 1 and arrival.hour == 9
    assert arrival.date() == dt.date(2026, 9, 22)


def test_next_arrival_rolls_forward_when_today_already_passed():
    commute = Commute(arrive_by="09:00", weekday="tuesday")
    now = dt.datetime(2026, 9, 22, 10, 0, tzinfo=PRAGUE)  # Tuesday, after 09:00
    assert commute.next_arrival(now).date() == dt.date(2026, 9, 29)


def test_config_requires_contact(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"office": {"lat": 1, "lon": 2}, "profiles": [{"name": "p"}]}))
    with pytest.raises(ValueError, match="contact"):
        config_mod.load(path)


def test_config_requires_office_coordinates(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"contact": "a@b", "office": {"name": "x"},
                                    "profiles": [{"name": "p"}]}))
    with pytest.raises(ValueError, match="office.lat"):
        config_mod.load(path)


def test_config_rejects_duplicate_profile_names(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"contact": "a@b", "office": {"lat": 1, "lon": 2},
                                    "profiles": [{"name": "p"}, {"name": "p"}]}))
    with pytest.raises(ValueError, match="unique"):
        config_mod.load(path)


def test_unknown_yaml_keys_are_ignored(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"contact": "a@b", "office": {"lat": 1, "lon": 2},
                                    "profiles": [{"name": "p", "future_option": 1}]}))
    assert config_mod.load(path).profiles[0].name == "p"


def test_user_agent_carries_contact(cfg):
    assert cfg.contact in cfg.user_agent and "ap-searcher" in cfg.user_agent


def test_save_weights_preserves_other_settings(cfg):
    """Values are patched in place: weights not passed in are left as they are."""
    before = yaml.safe_load(cfg.path.read_text())["scoring"]["weights"]
    config_mod.save_weights(cfg.path, {"commute": 2.0})

    raw = yaml.safe_load(cfg.path.read_text())
    assert raw["scoring"]["weights"]["commute"] == 2.0
    assert raw["scoring"]["weights"]["price"] == before["price"]
    assert raw["office"]["lat"] == cfg.office.lat
    assert raw["profiles"]


BBOX = {"lat_min": 50.07157419393176, "lat_max": 50.12140357700609,
        "lon_min": 14.42745066703636, "lon_max": 14.480236540449447}


def _write(tmp_path, profile_extra):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({
        "contact": "a@b", "office": {"lat": 1, "lon": 2},
        "profiles": [dict({"name": "p"}, **profile_extra)],
    }))
    return path


def test_bounding_box_is_loaded(tmp_path):
    profile = config_mod.load(_write(tmp_path, BBOX)).profiles[0]
    assert profile.has_bbox
    assert profile.lat_min == BBOX["lat_min"] and profile.lon_max == BBOX["lon_max"]


def test_profile_without_bounding_box(tmp_path):
    assert not config_mod.load(_write(tmp_path, {})).profiles[0].has_bbox


def test_partial_bounding_box_is_rejected(tmp_path):
    partial = {k: v for k, v in BBOX.items() if k != "lon_max"}
    with pytest.raises(ValueError, match="lon_max"):
        config_mod.load(_write(tmp_path, partial))


def test_inverted_bounding_box_is_rejected(tmp_path):
    inverted = dict(BBOX, lat_min=BBOX["lat_max"], lat_max=BBOX["lat_min"])
    with pytest.raises(ValueError, match="inverted"):
        config_mod.load(_write(tmp_path, inverted))


def test_shipped_config_bounding_box_is_valid():
    profile = config_mod.load(ROOT_CONFIG).profiles[0]
    assert profile.has_bbox
    assert profile.lat_min < profile.lat_max and profile.lon_min < profile.lon_max


def test_save_weights_keeps_comments_and_layout(tmp_path):
    src = (
        "# top of file\n"
        "profiles:\n"
        "- name: a          # a profile\n"
        "  deal: pronajem\n"
        "scoring:\n"
        "  weights:\n"
        "    commute: 1.0   # trailing note\n"
        "    price: 0.6\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(src, encoding="utf-8")

    config_mod.save_weights(path, {"commute": 2.5, "price": 0.1, "parking": 0.4})

    out = path.read_text(encoding="utf-8")
    assert "# top of file" in out and "# a profile" in out and "# trailing note" in out
    weights = yaml.safe_load(out)["scoring"]["weights"]
    assert weights == {"commute": 2.5, "price": 0.1, "parking": 0.4}


def test_save_weights_falls_back_when_there_is_no_weights_block(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("contact: me@example.com\n", encoding="utf-8")

    config_mod.save_weights(path, {"commute": 1.5})

    assert yaml.safe_load(path.read_text())["scoring"]["weights"] == {"commute": 1.5}


def test_furnished_accepts_the_yaml_boolean_spelling(tmp_path):
    """A bare `no` in YAML is boolean false; someone hand-editing will write that."""
    raw = yaml.safe_load(ROOT_CONFIG.read_text(encoding="utf-8"))
    raw["profiles"] = [{"name": "p", "locality": "praha-9", "furnished": [False, "partly"]}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    assert config_mod.load(path).profiles[0].furnished == ["no", "partly"]


def test_unknown_furnished_state_is_rejected(tmp_path):
    raw = yaml.safe_load(ROOT_CONFIG.read_text(encoding="utf-8"))
    raw["profiles"] = [{"name": "p", "locality": "praha-9", "furnished": ["sort-of"]}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="furnished must be any of"):
        config_mod.load(path)


def _profile_cfg(tmp_path, profile):
    raw = yaml.safe_load(ROOT_CONFIG.read_text(encoding="utf-8"))
    raw["profiles"] = [profile]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_areas_let_one_profile_cover_several_localities(tmp_path):
    path = _profile_cfg(tmp_path, {
        "name": "p",
        "areas": [
            {"locality": "praha-9"},
            {"locality": "praha-7", "neighborhoods": ["Holešovice"]},
        ],
    })
    areas = config_mod.load(path).profiles[0].search_areas
    assert [(a.locality, a.neighborhoods) for a in areas] == [
        ("praha-9", []), ("praha-7", ["Holešovice"])
    ]
    assert areas[1].neighborhood_slugs == {"holesovice"}


def test_locality_without_areas_is_the_single_area_shorthand(tmp_path):
    path = _profile_cfg(tmp_path, {"name": "p", "locality": "praha-3",
                                   "neighborhoods": ["Žižkov"]})
    areas = config_mod.load(path).profiles[0].search_areas
    assert [(a.locality, a.neighborhoods) for a in areas] == [("praha-3", ["Žižkov"])]


def test_areas_and_locality_together_are_rejected(tmp_path):
    path = _profile_cfg(tmp_path, {"name": "p", "locality": "praha-9",
                                   "areas": [{"locality": "praha-7"}]})
    with pytest.raises(ValueError, match="both 'areas' and 'locality'"):
        config_mod.load(path)
