# CLAUDE.md — ap-searcher

Operational notes for working on this codebase. User-facing documentation is in `README.md`.

## Layout

```
ap_searcher/
  __main__.py     entry point: argparse, logging, ThreadingHTTPServer
  server.py       Api (endpoint logic, testable) + Handler (HTTP shell) + build_view
  jobs.py         JobManager: one worker thread, status dict, cancel Event
  pipeline.py     run(): search → diff → details → amenities → routing
  sreality.py     URL building, __NEXT_DATA__ extraction, flattening, filter assertion
  amenities.py    three-state detection (structured params + folded-text regex)
  routing.py      MOTIS client, route keys, itinerary summarising, haversine
  http.py         Fetcher: TTL cache + rate limit + retries + budget + counters
  cache.py        Store: SQLite schema and all data access
  config.py       dataclasses, YAML loading/validation, arrival-time logic
  templates/      index.html.j2 (Jinja2)
  static/         app.js (fetch, score, filter, sort, render), app.css
tests/            pytest, fully offline
fixtures/         saved __NEXT_DATA__ payloads for search and detail pages
```

Scoring lives **only** in `static/app.js` — the weight sliders re-score client-side, and a second
Python implementation would drift. The server ships raw numbers plus weights.

## Invariants and gotchas

- **`__NEXT_DATA__` is the data source.** Search payload sits in the react-query entry whose
  `queryHash` starts `["estatesSearch"`; detail in `["estate"`. Do not add HTML scraping.
- **Always assert the echoed filters.** `sreality.check_filters()` compares the profile against the
  `queryHash` sreality returns. Unknown parameters *and* unparseable values are dropped silently by
  the site (`?ctvrt=` here, or a numeric `?vybaveni=1`), so without this a run looks fine but
  searches something else.
- **Codebook filters take seo names in, numbers out.** `?vybaveni=` wants `vybaveny` /
  `nevybaveny` / `castecne` (`sreality.FURNISHED_QUERY`) and a number is ignored without complaint;
  the `queryHash` then echoes the enum as `furnished: [2, 3]` (`FURNISHED_CODES`, the same values
  `params.furnished` uses on the detail page). Any future codebook filter — `stav`, `vlastnictvi`,
  `navic` — works the same way, so read the token list out of the site's JS bundle rather than
  guessing: `addArrayCodebookQueryParam(["vybaveni","furnished"], ...)` is the shape to grep for.
- **Bounding box params are locale-independent.** `lat-min/lat-max/lon-min/lon-max` work on the
  `cs` `/hledani/...` URLs, not just the localised `/ru|/en/search/...` ones, and come back in the
  `queryHash` as `latMin/latMax/lonMin/lonMax`. All four are required — sreality ignores a partial
  box, and `config.load` rejects one. Verified: 1033 results → 141 with the box applied.
- **There is no city-part filter on sreality's listing search.** `ctvrt=<seo-name>-<ward-id>`
  exists only on the `cenova-mapa` route; on `/hledani/` it is silently dropped, and
  `praha-9-vysocany`-style path slugs 404 (the route builder handles region/district/municipality
  only). `Profile.neighborhoods` is therefore applied by us in `pipeline._search` via
  `sreality.matches_neighborhoods`, against `locality.cityPart*`/`quarter*` in the search payload.
  The profile must be scoped to the containing district and given enough `max_pages` to sweep it,
  or matches on later pages are lost and `mark_removed` sees an incomplete `seen_ids`.
  Ward ids, if ever needed, come from `GET /api/v1/localities/suggest?phrase=<name>` (the only
  sreality JSON endpoint still answering 200 — Vysočany is ward 14969).
- **A profile can span several localities.** `Profile.search_areas` is the only thing the pipeline
  should iterate — it returns `profile.areas` or, for the common single-area profile, one `Area`
  built from `profile.locality`/`neighborhoods`. `build_search_url` and `check_filters` both take
  the area as their third argument and default to the first one. Removal detection is per profile
  but needs *all* of its areas swept to the end (`total <= max_pages * 22` for each), otherwise a
  listing sitting past the last fetched page of one area looks like it vanished.
- **`marks` carries two independent flags.** `state` is the exclusive shortlist/hide choice
  (`''` = neither, never NULL) and `viewed` is a separate column on the same row. `set_mark(...,
  "clear")` blanks the state and note but keeps a row that is still `viewed`; `set_viewed(id,
  False)` deletes a row that carries nothing else. Do not reintroduce the unconditional
  `DELETE FROM marks`.
- **`furnished` value 0 is "not filled in", not "no".** `sreality.furnished_state()` maps
  1/2/3 → yes/no/partly and everything else → `"unknown"`; roughly a third of ads sit there.
  The column is `NULL` only before derivation, so the backfill pass can tell "never derived" from
  "derived and blank".
- **Publication dates live on the detail page only.** `params.since` (published) and
  `params.edited` (last change); the search payload carries no date at all, only the site's default
  `sort=-date` ordering. Both are copied onto `listings` when a detail is fetched, and the
  re-derivation pass in `pipeline._details` backfills them from already-cached details without
  network.
- **`price_history` is sparse, not a time series.** `upsert_listing` appends a row only when the
  listing is new or its price actually moved, so two rows mean one change, not two sightings.
  `build_view` derives `price_delta` from the last two rows, which is why the `↓/↑` badge and the
  *price changed* filter stay on until the price moves again rather than clearing after one sweep.
  A per-sweep "changed just now" flag would need a different source — the `price_changes` list the
  run result already returns.
- **Detail URLs tolerate stale slugs.** Any slug with the right id 301s to the canonical URL;
  `requests` follows it. The id is the only part that must be right.
- **Route cache keys are coordinates, not listing ids.** Two ads in the same building share a key.
  `build_view` therefore looks routes up by key (`routes_by_key`), never by `listing_id`. The
  `routes.listing_id` column only records who paid for the request first.
- **Walk route keys carry no arrival stamp.** `routing.stamp_for()` returns the arrival moment for
  transit and `TIMELESS` ("any") for walking, because walking time cannot depend on the timetable.
  Without this every walk was recomputed when the arrival date rolled to the next week — roughly
  doubling the weekly request volume. `Store.routes_by_key()` therefore takes no arrival argument:
  the key encodes everything, and stale transit rows are simply never looked up.
- **MOTIS walking needs `maxDirectTime`.** The default (3600 s) silently returns zero `direct`
  results for longer walks. `Router.walk` sends 10800.
- **MOTIS responses are large (~100 KB)** and are never put in `http_cache`; the routing `Fetcher`
  runs with `use_cache=False` and the parsed summary goes into the `routes` table.
- **HTTP bodies are deflated** in `http_cache` (`zlib`). `http_get` still accepts plain `str` rows
  written by older versions.
- **Amenity text matching runs on `amenities.fold()`**, which strips diacritics while preserving
  character offsets, so match spans still index into the original Czech for the evidence snippet.
  Patterns are therefore written without diacritics.
- **Bare `stání` must never be a parking pattern** — it matches street names such as
  *náměstí Povstání*. Only qualified phrases count.
- **Image URLs need `?fl=res,100,100,1|jpg,80`** (`sreality.IMAGE_TRANSFORM`); other sizes are
  rejected by the CDN and bare URLs answer 401.
- **Profile selections travel as comma-separated names.** `RunOptions.profile_names()` and
  `build_view` both accept one name, a comma-separated list, or a list; `config.load` rejects a
  comma inside a profile name so the separator stays unambiguous. An empty selection means every
  profile *and* every orphaned listing — `store.listings([])` applies no `WHERE` at all, so rows
  from profiles that were renamed out of the config still show up. The UI relies on that
  difference, so do not silently expand "all boxes ticked" into "no filter".
- **`save_weights` never round-trips the YAML.** `yaml.safe_load` + `safe_dump` drops every
  comment in `config.yaml`, which is hand-edited and documented; `_patch_weights_block` rewrites
  only the `key: value` lines under `scoring.weights` (keeping trailing comments), appends weights
  that are missing, and falls back to a dump only when there is no weights block at all. Values not
  passed in are left alone, so a partial save cannot silently delete a weight.
- **One writer.** SQLite is in WAL mode and only the single job thread writes. Each thread opens
  its own `Store`; do not share connections across threads.
- **Transitous fair use.** Keep the contact in the `User-Agent`, the rate limit, and the per-run
  budget. Anything that would multiply routing volume needs the self-hosted MOTIS path instead.

## Dev workflow

```bash
python3 -m pytest tests -q          # offline, ~7 s
python3 -m ap_searcher --debug      # server with verbose logging
```

There is no virtualenv: `requests`, `PyYAML` and `Jinja2` come from the distro packages (Flask is
deliberately not used — `pip` is blocked by PEP 668 on this machine and `python3-venv` is absent).

Live checks that do hit the network:

```bash
# one page, no routing
python3 -c "import sys;sys.path.insert(0,'.');
from ap_searcher import config;from ap_searcher.cache import Store;from ap_searcher.pipeline import *
cfg=config.load('config.yaml');
print(run(cfg, RunOptions(max_pages=1, do_routing=False), Store(cfg.cache.path)).as_dict())"
```

Re-running immediately must report `requests: 0` for sreality — that is the cache regression test.

## Changing things

- **New search filter:** add the field to `Profile`, emit the query parameter in
  `build_search_url`, and assert it in `check_filters`. Verify the parameter name by loading the
  URL in a browser and reading the `queryHash` back. If the `queryHash` comes back unchanged the
  site does not support the filter at all; the fallback is a post-filter in `pipeline._search`
  (see `neighborhoods`), which costs pages fetched and thrown away.
- **New structured detail field:** map it in `sreality.extract_detail`, add a column to
  `cache.SCHEMA` *and* `cache.MIGRATIONS`, a setter on `Store`, a write in `pipeline._details`
  plus a `needs_*` branch in the re-derivation pass (so existing rows backfill without network),
  a key in `build_view`, and the UI bits. `furnished` is the worked example.
- **New amenity:** add a key to `amenities.KEYS`, patterns to `TEXT_RULES` (folded, negation-safe),
  any structured fields to `STRUCTURED_RULES`, then a column *and* a filter checkbox in
  `index.html.j2`, `AMENITY_KEYS` in `app.js`, and a default weight in `config.py`/`config.yaml`.
  Existing rows backfill on the next *Fetch details* without network, because `_details` re-derives
  any listing whose stored keys do not cover `amen.KEYS` — do not weaken that test back to
  "listing has no amenity rows at all" or the new column reads `?` until a forced refresh. Check a
  new text rule against the cached corpus before trusting it (`SELECT description FROM details`);
  `cellar` picked up `Sklep: Není` and `nenáleží sklep` that way.
- **New routing provider:** implement `transit()` / `walk()` with the same return dict
  (`status`, `duration_s`, `transfers`, `summary`, `legs`) and swap it in `pipeline._routing`.
- **Second listing source:** produce the `listing_from_result` dict shape and call
  `store.upsert_listing`; the rest of the pipeline is source-agnostic.
- **Schema change:** `cache.SCHEMA` runs with `CREATE TABLE IF NOT EXISTS` on every connect, which
  only covers new databases. For an added column, append it to `SCHEMA` *and* to `cache.MIGRATIONS`
  (`(table, column, decl)`); `migrate()` runs on connect, compares `PRAGMA table_info`, and is
  idempotent. There is no version table — migrations must stay additive and re-runnable. Data
  migrations go in the same function (see `_rekey_timeless_walks`), guarded so a second run is a
  no-op.

## Regenerating fixtures

`fixtures/*.json` are trimmed `__NEXT_DATA__` payloads (only the relevant query kept). Refresh them
when sreality changes shape; `tests/conftest.py` wraps them back into a minimal HTML page.
