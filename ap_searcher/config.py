"""Configuration loading and validation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

PRAGUE = ZoneInfo("Europe/Prague")

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


@dataclass
class Office:
    lat: float
    lon: float
    name: str = "office"


@dataclass
class Commute:
    arrive_by: str = "09:00"
    weekday: str = "tuesday"
    walk_max_km: float = 5.0

    def next_arrival(self, now: dt.datetime | None = None) -> dt.datetime:
        """Next occurrence of the configured weekday/time, in Europe/Prague."""
        now = now or dt.datetime.now(PRAGUE)
        if now.tzinfo is None:
            now = now.replace(tzinfo=PRAGUE)
        hour, minute = (int(x) for x in self.arrive_by.split(":"))
        target = WEEKDAYS[self.weekday.lower()]
        days = (target - now.weekday()) % 7
        candidate = (now + dt.timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += dt.timedelta(days=7)
        return candidate


@dataclass
class Area:
    """One locality to sweep, optionally narrowed to some of its city parts."""

    locality: str = "praha"
    neighborhoods: list[str] = field(default_factory=list)

    @property
    def neighborhood_slugs(self) -> set[str]:
        from .sreality import neighborhood_slug

        return {neighborhood_slug(n) for n in self.neighborhoods if n}


@dataclass
class Profile:
    name: str
    deal: str = "pronajem"
    kind: str = "byty"
    locality: str = "praha"
    price_from: int | None = None
    price_to: int | None = None
    area_from: int | None = None
    area_to: int | None = None
    dispositions: list[str] = field(default_factory=list)
    # City parts to keep, e.g. ["Vysočany"]. Applied by us after the search, because
    # sreality's listing search has no ward filter - see sreality.matches_neighborhoods.
    neighborhoods: list[str] = field(default_factory=list)
    # Furnishing states to search for: any of "yes", "no", "partly". Applied by sreality
    # itself (?vybaveni=), so ads that leave the field blank are excluded server-side.
    furnished: list[str] = field(default_factory=list)
    # Several localities in one profile, e.g. all of Praha 9 plus Holešovice in Praha 7.
    # `locality`/`neighborhoods` above are the shorthand for a profile with one area.
    areas: list["Area"] = field(default_factory=list)
    # Map bounding box, as sreality's lat-min/lat-max/lon-min/lon-max URL params.
    lat_min: float | None = None
    lat_max: float | None = None
    lon_min: float | None = None
    lon_max: float | None = None
    max_pages: int = 5

    @property
    def search_areas(self) -> list[Area]:
        return self.areas or [Area(self.locality, self.neighborhoods)]

    @property
    def has_bbox(self) -> bool:
        return any(v is not None for v in (self.lat_min, self.lat_max, self.lon_min, self.lon_max))


@dataclass
class RoutingConfig:
    base_url: str = "https://api.transitous.org"
    min_interval_s: float = 1.0
    max_requests_per_run: int = 200


@dataclass
class SrealityConfig:
    min_interval_s: float = 1.5


@dataclass
class CacheConfig:
    path: str = "cache.sqlite3"
    search_ttl_h: float = 6
    detail_ttl_d: float = 14
    walk_ttl_d: float = 90
    transit_ttl_d: float = 7


@dataclass
class Config:
    contact: str
    office: Office
    profiles: list[Profile]
    server: ServerConfig = field(default_factory=ServerConfig)
    commute: Commute = field(default_factory=Commute)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    sreality: SrealityConfig = field(default_factory=SrealityConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    weights: dict[str, float] = field(default_factory=dict)
    path: Path | None = None

    def profile(self, name: str) -> Profile:
        for p in self.profiles:
            if p.name == name:
                return p
        raise KeyError(f"unknown profile: {name}")

    @property
    def user_agent(self) -> str:
        from . import __version__

        return f"ap-searcher/{__version__} (+{self.contact}; personal apartment search)"


DEFAULT_WEIGHTS = {
    "commute": 1.0,
    "price": 0.6,
    "area": 0.3,
    "parking": 0.4,
    "dishwasher": 0.3,
    "induction": 0.2,
    "bathtub": 0.2,
    "cellar": 0.2,
}


def _subset(cls, data: dict[str, Any] | None):
    data = data or {}
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in known})


def load(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if not raw.get("contact"):
        raise ValueError("config: 'contact' is required (used in the User-Agent)")
    office_raw = raw.get("office") or {}
    for key in ("lat", "lon"):
        if office_raw.get(key) is None:
            raise ValueError(f"config: office.{key} is required")

    raw_profiles = raw.get("profiles") or []
    profiles = [_subset(Profile, p) for p in raw_profiles]
    if not profiles:
        raise ValueError("config: at least one search profile is required")
    for profile, raw_profile in zip(profiles, raw_profiles):
        if not profile.areas:
            continue
        # Two ways of saying where to search would be one too many.
        clash = [k for k in ("locality", "neighborhoods") if k in raw_profile]
        if clash:
            raise ValueError(
                f"config: profile {profile.name!r} sets both 'areas' and "
                f"{', '.join(repr(k) for k in clash)}; move them into the areas list"
            )
        profile.areas = [_subset(Area, a) for a in profile.areas]
    names = [p.name for p in profiles]
    if len(names) != len(set(names)):
        raise ValueError("config: profile names must be unique")
    for profile in profiles:
        # The HTTP API passes profile selections as a comma-separated list.
        if "," in profile.name:
            raise ValueError(f"config: profile name {profile.name!r} must not contain a comma")
        from .sreality import FURNISHED_QUERY

        # YAML 1.1 turns a bare `no`/`yes` into a boolean, which is what someone
        # hand-editing this file will write, so accept both spellings.
        profile.furnished = [
            {True: "yes", False: "no"}.get(f, f) if isinstance(f, bool) else f
            for f in profile.furnished
        ]
        unknown = [f for f in profile.furnished if f not in FURNISHED_QUERY]
        if unknown:
            raise ValueError(
                f"config: profile {profile.name!r} furnished must be any of "
                f"{sorted(FURNISHED_QUERY)}, got {unknown}"
            )
        for area in profile.search_areas:
            if not isinstance(area.neighborhoods, list) or not all(
                isinstance(n, str) and n.strip() for n in area.neighborhoods
            ):
                raise ValueError(
                    f"config: profile {profile.name!r} neighborhoods must be a list of names"
                )
            if not isinstance(area.locality, str) or not area.locality.strip():
                raise ValueError(f"config: profile {profile.name!r} needs a locality slug")
        if profile.has_bbox:
            missing = [
                name for name, value in (
                    ("lat_min", profile.lat_min), ("lat_max", profile.lat_max),
                    ("lon_min", profile.lon_min), ("lon_max", profile.lon_max),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"config: profile {profile.name!r} bounding box needs all four corners, "
                    f"missing: {', '.join(missing)}"
                )
            if profile.lat_min >= profile.lat_max or profile.lon_min >= profile.lon_max:
                raise ValueError(
                    f"config: profile {profile.name!r} bounding box is inverted "
                    "(lat_min/lon_min must be smaller than lat_max/lon_max)"
                )

    commute = _subset(Commute, raw.get("commute"))
    if commute.weekday.lower() not in WEEKDAYS:
        raise ValueError(f"config: commute.weekday must be one of {sorted(WEEKDAYS)}")

    weights = dict(DEFAULT_WEIGHTS)
    weights.update((raw.get("scoring") or {}).get("weights") or {})

    return Config(
        contact=raw["contact"],
        office=_subset(Office, office_raw),
        profiles=profiles,
        server=_subset(ServerConfig, raw.get("server")),
        commute=commute,
        routing=_subset(RoutingConfig, raw.get("routing")),
        sreality=_subset(SrealityConfig, raw.get("sreality")),
        cache=_subset(CacheConfig, raw.get("cache")),
        weights=weights,
        path=path,
    )


def save_weights(path: str | Path, weights: dict[str, float]) -> None:
    """Rewrite only the weight values, leaving comments and formatting untouched.

    A load/dump round-trip would be shorter but drops every comment in the file, and
    config.yaml is hand-edited and documented, so the values are patched in place.
    Falls back to a dump only if the weights block cannot be located.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    values = {k: float(v) for k, v in weights.items()}
    patched = _patch_weights_block(text, values)
    if patched is None:
        raw = yaml.safe_load(text) or {}
        raw.setdefault("scoring", {})["weights"] = values
        patched = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    path.write_text(patched, encoding="utf-8")


def _patch_weights_block(text: str, values: dict[str, float]) -> str | None:
    """Replace `key: number` lines inside scoring.weights; None if it is not there."""
    lines = text.splitlines(keepends=True)
    start = indent = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in ("weights:", "weights: {}") and line.startswith("  "):
            start, indent = i + 1, len(line) - len(line.lstrip())
            break
    if start is None:
        return None

    end = start
    written: set[str] = set()
    while end < len(lines):
        line = lines[end]
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        key = line.split(":", 1)[0].strip()
        if key in values:
            pad = line[: len(line) - len(line.lstrip())]
            after = line.split(":", 1)[1]
            comment = f"  {after[after.index('#'):].rstrip()}" if "#" in after else ""
            lines[end] = f"{pad}{key}: {values[key]}{comment}\n"
            written.add(key)
        end += 1

    pad = " " * (indent + 2)
    extra = [f"{pad}{k}: {v}\n" for k, v in values.items() if k not in written]
    return "".join(lines[:end] + extra + lines[end:])
