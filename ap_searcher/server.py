"""HTTP server: one page plus a small JSON API. Everything the tool does is a button.

Stdlib only (ThreadingHTTPServer + Jinja2) so the app runs with nothing to install.
The request handling is split in two: `Api` holds the endpoint logic and is directly
unit-testable, `Handler` is the thin HTTP shell around it.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import time
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config as config_mod
from . import routing
from .cache import Store
from .config import Config
from .jobs import JobManager
from .pipeline import RunOptions

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).parent
STATIC_DIR = PKG_DIR / "static"
NEW_WINDOW_S = 2 * 86400


def _route_view(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "status": row["status"],
        "minutes": round(row["duration_s"] / 60) if row["duration_s"] else None,
        "transfers": row["transfers"],
        "summary": row["summary"],
        "legs": json.loads(row["legs_json"] or "null"),
    }


def build_view(
    cfg: Config, store: Store, profiles: str | list[str] | None, include_hidden: bool
) -> dict:
    arrive = cfg.commute.next_arrival()
    arrive_key = arrive.isoformat()
    amenities = store.amenities_map()
    routes = store.routes_by_key()
    marks = store.marks_map()
    now = time.time()

    selected = profiles.split(",") if isinstance(profiles, str) else (profiles or [])
    selected = [name.strip() for name in selected if name and name.strip()]

    listings = []
    for row in store.listings(selected):
        mark = marks.get(row["id"], {})
        if mark.get("state") == "hide" and not include_hidden:
            continue
        history = store.price_history(row["id"])
        price_delta = None
        if len(history) >= 2 and history[-1]["price_czk"] and history[-2]["price_czk"]:
            price_delta = history[-1]["price_czk"] - history[-2]["price_czk"]
        def route_for(mode: str):
            if row["lat"] is None or row["lon"] is None:
                return None
            key = routing.route_key(row["lat"], row["lon"], cfg.office, mode, arrive_key)
            return routes.get(key)
        listings.append(
            {
                "id": row["id"],
                "title": row["title"],
                "url": row["url"],
                "image_url": row["image_url"],
                "deal": row["deal"],
                "disposition": row["disposition"],
                "area_m2": row["area_m2"],
                "price_czk": row["price_czk"],
                "price_unit": row["price_unit"],
                "price_per_m2": row["price_per_m2"],
                "city_part": row["city_part"],
                "district": row["district"],
                "street": row["street"],
                "lat": row["lat"],
                "lon": row["lon"],
                "profiles": store.profiles_of(row["id"]),
                "furnished": row["furnished"],
                "published_on": row["published_on"],
                "edited_on": row["edited_on"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "removed_at": row["removed_at"],
                "is_new": bool(row["first_seen"] and now - row["first_seen"] < NEW_WINDOW_S),
                "price_delta": price_delta,
                "amenities": amenities.get(row["id"], {}),
                "transit": _route_view(route_for(routing.TRANSIT)),
                "walk": _route_view(route_for(routing.WALK)),
                "mark": mark.get("state"),
                "viewed": bool(mark.get("viewed")),
                "note": mark.get("note"),
            }
        )

    return {
        "listings": listings,
        "arrive_by": arrive_key,
        "arrive_human": arrive.strftime("%a %d %b, %H:%M"),
        "office": asdict(cfg.office),
        "weights": cfg.weights,
        "profiles": [p.name for p in cfg.profiles],
        "selected_profiles": selected,
    }


class Api:
    """Endpoint logic, independent of the HTTP layer (and therefore testable)."""

    def __init__(self, cfg: Config, jobs: JobManager | None = None):
        self.cfg = cfg
        self.jobs = jobs or JobManager(cfg)
        self.env = Environment(
            loader=FileSystemLoader(str(PKG_DIR / "templates")),
            autoescape=select_autoescape(["html", "xml", "j2"]),
        )

    def store(self) -> Store:
        return Store(self.cfg.cache.path)

    # ------------------------------------------------------------------ routes

    def index_html(self) -> str:
        return self.env.get_template("index.html.j2").render(
            office=self.cfg.office,
            profiles=[p.name for p in self.cfg.profiles],
            default_pages=max(p.max_pages for p in self.cfg.profiles),
            default_budget=self.cfg.routing.max_requests_per_run,
            arrive_human=self.cfg.commute.next_arrival().strftime("%a %d %b, %H:%M"),
        )

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, Any]:
        key = (method, path)
        if key == ("GET", "/api/config"):
            return 200, {
                "office": asdict(self.cfg.office),
                "profiles": [asdict(p) for p in self.cfg.profiles],
                "weights": self.cfg.weights,
                "commute": asdict(self.cfg.commute),
                "routing_budget": self.cfg.routing.max_requests_per_run,
            }

        if key == ("GET", "/api/listings"):
            # `profile` takes one name or a comma-separated list; empty means all.
            with self.store() as s:
                return 200, build_view(
                    self.cfg, s, query.get("profile") or None, query.get("include_hidden") == "1"
                )

        if key == ("POST", "/api/run"):
            opts = RunOptions(
                profile=body.get("profile") or None,
                max_pages=body.get("max_pages") or None,
                do_search=bool(body.get("search", True)),
                do_details=bool(body.get("details", True)),
                do_routing=bool(body.get("routing", True)),
                force_refresh=bool(body.get("force", False)),
                routing_budget=body.get("budget") or None,
            )
            try:
                return 200, self.jobs.start(opts, label=body.get("label", "run"))
            except RuntimeError as exc:
                return 409, {"error": str(exc)}

        if key == ("GET", "/api/jobs/current"):
            return 200, self.jobs.status()

        if key == ("POST", "/api/jobs/cancel"):
            return 200, {"cancelled": self.jobs.cancel()}

        if key == ("POST", "/api/mark"):
            listing_id, state = body.get("id"), body.get("state")
            viewed = body.get("viewed")
            if listing_id is None:
                return 400, {"error": "id required"}
            # `viewed` is independent of the star/hide choice; either may be sent alone.
            if state is None and viewed is None:
                return 400, {"error": "state (star|hide|clear) or viewed required"}
            if state is not None and state not in ("star", "hide", "clear"):
                return 400, {"error": "state must be star|hide|clear"}
            with self.store() as s:
                if state is not None:
                    s.set_mark(int(listing_id), state, body.get("note"))
                if viewed is not None:
                    s.set_viewed(int(listing_id), bool(viewed))
            return 200, {"id": listing_id, "state": state, "viewed": viewed}

        if key == ("POST", "/api/weights"):
            weights = body.get("weights") or {}
            if not weights:
                return 400, {"error": "weights required"}
            self.cfg.weights.update({k: float(v) for k, v in weights.items()})
            if self.cfg.path:
                config_mod.save_weights(self.cfg.path, self.cfg.weights)
            return 200, {"weights": self.cfg.weights}

        if key == ("GET", "/api/cache"):
            with self.store() as s:
                return 200, s.stats()

        if key == ("DELETE", "/api/cache"):
            older = query.get("older_than_d")
            with self.store() as s:
                removed = s.purge(
                    query.get("kind", "http"), float(older) * 86400 if older else None
                )
            return 200, {"removed": removed, "kind": query.get("kind", "http")}

        return 404, {"error": f"no route for {method} {path}"}


class Handler(BaseHTTPRequestHandler):
    api: Api  # injected by make_server
    server_version = "ap-searcher"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _static(self, path: str) -> None:
        name = Path(path).name
        target = STATIC_DIR / name
        if not target.is_file() or target.parent != STATIC_DIR:
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if method == "GET" and path == "/":
            self._send(200, self.api.index_html().encode(), "text/html; charset=utf-8")
            return
        if method == "GET" and path.startswith("/static/"):
            self._static(path)
            return

        body: dict = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return
        try:
            status, payload = self.api.handle(method, path, query, body)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("API error on %s %s", method, path)
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self._json(status, payload)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


def make_server(cfg: Config, jobs: JobManager | None = None) -> ThreadingHTTPServer:
    api = Api(cfg, jobs)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer((cfg.server.host, cfg.server.port), handler)
    httpd.daemon_threads = True
    return httpd
