/* ap-searcher UI: fetch the view model, score it client-side, render a sortable table. */
"use strict";

const state = {
  data: null,
  weights: {},
  sort: { key: "score", dir: -1 },
  poll: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const fmt = new Intl.NumberFormat("cs-CZ");

/* ------------------------------------------------------------------ scoring */

const AMENITY_KEYS = ["parking", "dishwasher", "induction", "bathtub", "cellar"];
const AMENITY_VALUE = { yes: 1, maybe: 0.6, unknown: 0 };

function commuteMinutes(l) {
  const t = l.transit && l.transit.status === "ok" ? l.transit.minutes : null;
  const w = l.walk && l.walk.status === "ok" ? l.walk.minutes : null;
  if (t === null && w === null) return null;
  if (t === null) return w;
  if (w === null) return t;
  return Math.min(t, w);
}

function scoreAll(listings, weights) {
  const nums = (pick) => listings.map(pick).filter((v) => typeof v === "number" && !isNaN(v));
  const range = (vals) => {
    if (!vals.length) return null;
    const lo = Math.min(...vals), hi = Math.max(...vals);
    return hi === lo ? null : { lo, hi };
  };
  const rCommute = range(nums(commuteMinutes));
  const rPrice = range(nums((l) => l.price_czk));
  const rArea = range(nums((l) => l.area_m2));

  const norm = (v, r, invert) => {
    if (v === null || v === undefined || isNaN(v)) return null;
    if (!r) return 0.5;
    const x = (v - r.lo) / (r.hi - r.lo);
    return invert ? 1 - x : x;
  };

  for (const l of listings) {
    let sum = 0, wsum = 0, parts = [];
    const add = (key, value) => {
      const w = weights[key] || 0;
      if (!w || value === null) return;
      sum += w * value; wsum += w;
      parts.push(`${key} ${(value * 100).toFixed(0)}% × ${w}`);
    };
    add("commute", norm(commuteMinutes(l), rCommute, true));
    add("price", norm(l.price_czk, rPrice, true));
    add("area", norm(l.area_m2, rArea, false));
    for (const key of AMENITY_KEYS) {
      const st = (l.amenities[key] || {}).state || "unknown";
      add(key, AMENITY_VALUE[st]);
    }
    l.score = wsum ? Math.round((sum / wsum) * 100) : 0;
    l.score_breakdown = parts.join("\n");
  }
}

/* ------------------------------------------------------------------ filtering */

function activeFilters() {
  return {
    transit: numOrNull($("#f-transit").value),
    walk: numOrNull($("#f-walk").value),
    priceMin: numOrNull($("#f-price-min").value),
    price: numOrNull($("#f-price").value),
    area: numOrNull($("#f-area").value),
    text: $("#f-text").value.trim().toLowerCase(),
    amenities: $$("[data-amen]").filter((c) => c.checked).map((c) => c.dataset.amen),
    strict: $("#f-strict").checked,
    furnished: [
      ...($("#f-furnished").checked ? ["yes"] : []),
      ...($("#f-partly").checked ? ["partly"] : []),
      ...($("#f-unfurnished").checked ? ["no"] : []),
    ],
    isNew: $("#f-new").checked,
    priceChanged: $("#f-price-changed").checked,
    starred: $("#f-starred").checked,
    showHidden: $("#f-hidden").checked,
    activeOnly: $("#f-active").checked,
  };
}

const numOrNull = (v) => (v === "" || v === null ? null : Number(v));

function applyFilters(listings, f) {
  return listings.filter((l) => {
    if (f.activeOnly && l.removed_at) return false;
    if (f.starred && l.mark !== "star") return false;
    if (f.isNew && !l.is_new) return false;
    if (f.priceChanged && !l.price_delta) return false;
    if (!f.showHidden && l.mark === "hide") return false;
    if (f.price !== null && (l.price_czk || Infinity) > f.price) return false;
    if (f.priceMin !== null && (l.price_czk || 0) < f.priceMin) return false;
    if (f.area !== null && (l.area_m2 || 0) < f.area) return false;
    if (f.transit !== null) {
      const t = l.transit && l.transit.status === "ok" ? l.transit.minutes : null;
      if (t === null || t > f.transit) return false;
    }
    if (f.walk !== null) {
      const w = l.walk && l.walk.status === "ok" ? l.walk.minutes : null;
      if (w === null || w > f.walk) return false;
    }
    // no box ticked = no constraint; otherwise the state must be one of the ticked ones,
    // which also drops listings whose ad left the field blank
    if (f.furnished.length && !f.furnished.includes(l.furnished)) return false;
    for (const key of f.amenities) {
      const st = (l.amenities[key] || {}).state || "unknown";
      if (f.strict ? st !== "yes" : st === "unknown") return false;
    }
    if (f.text) {
      const hay = [l.title, l.city_part, l.district, l.street, l.disposition]
        .filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(f.text)) return false;
    }
    return true;
  });
}

const DATE_FMT = new Intl.DateTimeFormat("cs-CZ", { day: "numeric", month: "short" });

function publishedCell(l) {
  if (!l.published_on) return '<td class="date" title="not fetched yet">–</td>';
  const date = new Date(l.published_on);
  const days = Math.floor((Date.now() - date.getTime()) / 86400000);
  const age = days <= 0 ? "today" : days === 1 ? "1 day ago" : `${days} days ago`;
  const tip = [`published ${l.published_on} (${age})`,
               l.edited_on ? `edited ${l.edited_on}` : null].filter(Boolean).join("\n");
  return `<td class="date" title="${escapeAttr(tip)}">${DATE_FMT.format(date)}</td>`;
}

function sortValue(l, key) {
  switch (key) {
    // ISO dates sort lexicographically
    case "published_on": return l.published_on || null;
    case "transit": return l.transit && l.transit.status === "ok" ? l.transit.minutes : null;
    case "walk": return l.walk && l.walk.status === "ok" ? l.walk.minutes : null;
    default: return l[key] ?? null;
  }
}

const isMissing = (v) => v === null || v === undefined || v === "" || (typeof v === "number" && isNaN(v));

/* Missing values always sink to the bottom, whichever direction is active - a listing
   with no route should never head the commute sort just because it sorts descending. */
function compareBy(key, dir) {
  return (a, b) => {
    const va = sortValue(a, key), vb = sortValue(b, key);
    const ma = isMissing(va), mb = isMissing(vb);
    if (ma || mb) return ma && mb ? 0 : ma ? 1 : -1;
    if (typeof va === "string" || typeof vb === "string") {
      return String(va).localeCompare(String(vb), "cs") * dir;
    }
    return (va - vb) * dir;
  };
}

/* ------------------------------------------------------------------ rendering */

const TICK = { yes: "✓", maybe: "~", unknown: "·" };
const FURNISHED_LABEL = { yes: "furnished", partly: "partly furnished", no: "unfurnished" };

// Transfers, not minutes, are what makes a commute tiring, so the transit column is
// tinted by them: 0 green, 1 yellow, 2 orange, 3+ red. An unknown count stays plain.
function transferClass(transfers) {
  if (transfers === null || transfers === undefined) return "";
  return ` tr${Math.min(transfers, 3)}`;
}

function routeCell(route, colorByTransfers = false) {
  if (!route) return '<td class="num" title="not computed yet">–</td>';
  if (route.status === "too_far") return `<td class="num" title="${route.summary || "beyond walk radius"}">–</td>`;
  if (route.status !== "ok") return '<td class="num" title="no route found">×</td>';
  const tip = [route.summary, route.transfers !== null ? `${route.transfers} transfer(s)` : null]
    .filter(Boolean).join("\n");
  const cls = colorByTransfers ? transferClass(route.transfers) : "";
  return `<td class="num${cls}" title="${escapeAttr(tip)}">${route.minutes}′</td>`;
}

function render() {
  if (!state.data) return;
  const listings = state.data.listings;
  scoreAll(listings, state.weights);
  const filtered = applyFilters(listings, activeFilters());
  filtered.sort(compareBy(state.sort.key, state.sort.dir));

  $("#count").textContent = `${filtered.length} of ${listings.length} listings`;
  $("#rows").innerHTML = filtered.map(rowHtml).join("");
  $$("#rows button[data-act]").forEach((btn) => {
    const id = Number(btn.dataset.id);
    btn.addEventListener("click", () =>
      btn.dataset.act === "viewed" ? toggleViewed(id) : mark(id, btn.dataset.act));
  });
}

function rowHtml(l) {
  const badges = [];
  if (l.is_new) badges.push('<span class="badge new">NEW</span>');
  if (l.price_delta) {
    const cls = l.price_delta < 0 ? "down" : "up";
    const sign = l.price_delta < 0 ? "↓" : "↑";
    badges.push(`<span class="badge ${cls}">${sign} ${fmt.format(Math.abs(l.price_delta))}</span>`);
  }
  if (l.removed_at) badges.push('<span class="badge gone">GONE</span>');

  const amenityCells = AMENITY_KEYS.map((key) => {
    const a = l.amenities[key] || { state: "unknown" };
    const tip = a.evidence || `${key}: no evidence`;
    return `<td class="amen"><span class="tick ${a.state}" title="${escapeAttr(tip)}">${TICK[a.state]}</span></td>`;
  }).join("");

  const loc = [l.city_part, l.street].filter(Boolean).join(", ");
  const price = l.price_czk ? `${fmt.format(l.price_czk)}` : "–";

  return `<tr class="${[l.mark === "hide" ? "hidden-row" : "", l.viewed ? "viewed-row" : ""].filter(Boolean).join(" ")}">
    <td class="thumb">${l.image_url ? `<img loading="lazy" src="${escapeAttr(l.image_url)}" alt="">` : ""}</td>
    <td class="title">
      <a href="${escapeAttr(l.url)}" target="_blank" rel="noreferrer">${escape(l.title || l.id)}</a>
      <span class="badges">${badges.join("")}</span>
      ${l.note ? `<div class="sub-line">${escape(l.note)}</div>` : ""}
    </td>
    <td>${escape(l.disposition || "")}${
      FURNISHED_LABEL[l.furnished]
        ? `<div class="sub-line">${FURNISHED_LABEL[l.furnished]}</div>`
        : ""
    }</td>
    <td class="num">${l.area_m2 ? fmt.format(l.area_m2) : "–"}</td>
    <td class="num" title="${escapeAttr(l.price_unit || "")}">${price}</td>
    <td class="num">${l.price_per_m2 ? fmt.format(l.price_per_m2) : "–"}</td>
    <td>${escape(loc)}<div class="sub-line">${escape(l.district || "")}</div></td>
    ${publishedCell(l)}
    ${routeCell(l.transit, true)}
    ${routeCell(l.walk)}
    ${amenityCells}
    <td class="num score" title="${escapeAttr(l.score_breakdown || "")}">${l.score}</td>
    <td>
      <div class="acts">
        <button data-act="star" data-id="${l.id}" class="${l.mark === "star" ? "on" : ""}" title="shortlist">★</button>
        <button data-act="hide" data-id="${l.id}" class="${l.mark === "hide" ? "on" : ""}" title="hide">✕</button>
        <button data-act="viewed" data-id="${l.id}" class="${l.viewed ? "on" : ""}" title="viewed">👁</button>
      </div>
    </td>
  </tr>`;
}

const escape = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const escapeAttr = (s) => escape(s).replace(/"/g, "&quot;");

/* ------------------------------------------------------------------ API calls */

async function api(path, options) {
  const resp = await fetch(path, options);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

// Profile selection: a set of checkboxes, none ticked meaning "all profiles".
function selectedProfiles() {
  return $$(".profile-opt").filter((c) => c.checked).map((c) => c.value);
}

function renderProfileButton() {
  const picked = selectedProfiles();
  // Ticking every box is NOT the same as ticking none: an empty selection also shows
  // listings left behind by profiles that are no longer in config.yaml.
  const label =
    picked.length === 0 ? "all profiles"
      : picked.length === 1 ? picked[0]
        : `${picked.length} profiles`;
  $("#profile-btn").textContent = `${label} ▾`;
}

async function loadListings() {
  const picked = selectedProfiles();
  const params = new URLSearchParams();
  if (picked.length) params.set("profile", picked.join(","));
  params.set("include_hidden", "1");
  state.data = await api(`/api/listings?${params}`);
  state.weights = Object.assign({}, state.data.weights, state.weights);
  renderWeights();
  render();
  loadCacheStats();
}

async function loadCacheStats() {
  const s = await api("/api/cache");
  const mb = (s.db_bytes / 1048576).toFixed(1);
  $("#cache-stats").textContent =
    `cache: ${s.active_listings}/${s.listings} listings · ${s.details} details · ${s.routes} routes · ${s.http_cache} pages · ${mb} MB`;
}

async function mark(id, act) {
  const listing = state.data.listings.find((l) => l.id === id);
  const next = listing.mark === act ? "clear" : act;
  await api("/api/mark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, state: next }),
  });
  listing.mark = next === "clear" ? null : next;
  render();
}

async function toggleViewed(id) {
  const listing = state.data.listings.find((l) => l.id === id);
  const next = !listing.viewed;
  await api("/api/mark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, viewed: next }),
  });
  listing.viewed = next;
  render();
}

async function startRun(payload, label) {
  try {
    await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ label }, payload)),
    });
    pollJob();
  } catch (err) {
    setStatus(`error: ${err.message}`, 0);
  }
}

function runPayload(extra) {
  return Object.assign(
    {
      profile: selectedProfiles().join(",") || null,
      max_pages: Number($("#max-pages").value) || null,
      budget: Number($("#budget").value) || null,
      force: $("#force").checked,
    },
    extra
  );
}

function setStatus(text, progress) {
  $("#status-line").textContent = text;
  $("#bar-fill").style.width = `${Math.round((progress || 0) * 100)}%`;
}

function pollJob() {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    const job = await api("/api/jobs/current");
    const running = job.state === "running" || job.state === "cancelling";
    $("#btn-cancel").disabled = !running;
    $$(".btns button:not(#btn-cancel)").forEach((b) => (b.disabled = running));

    if (running) {
      const frac = job.total ? job.done / job.total : 0;
      setStatus(
        `${job.label} · ${job.stage} ${job.done}/${job.total} · ${job.message || ""}`.trim(),
        frac
      );
      return;
    }

    clearInterval(state.poll);
    state.poll = null;
    if (job.state === "failed") {
      setStatus(`failed: ${job.error}`, 0);
    } else if (job.result) {
      const r = job.result;
      const c = r.counters || {};
      setStatus(
        `${job.state} · ${r.new_count} new · ${r.price_changes.length} price changes · ` +
        `details ${r.details_fetched} new/${r.details_cached} cached · ` +
        `routes ${r.routes_computed} new/${r.routes_cached} cached · ` +
        `sreality ${c.sreality.requests} req, ${c.sreality.cache_hits} from cache · ` +
        `routing ${c.routing.requests} req`,
        1
      );
      $("#warn-line").hidden = !r.warnings.length;
      $("#warn-line").textContent = r.warnings.slice(0, 8).join("\n");
    } else {
      setStatus("idle", 0);
    }
    loadListings();
  }, 900);
}

/* ------------------------------------------------------------------ weights UI */

function renderWeights() {
  const panel = $("#weights-panel");
  panel.innerHTML =
    Object.entries(state.weights).map(([key, value]) =>
      `<div class="w"><label>${key} <output id="out-${key}">${value}</output></label>
       <input type="range" min="0" max="2" step="0.1" value="${value}" data-weight="${key}"></div>`
    ).join("") + '<button id="btn-save-weights" class="ghost">Save to config.yaml</button>';

  panel.querySelectorAll("[data-weight]").forEach((slider) => {
    slider.addEventListener("input", () => {
      state.weights[slider.dataset.weight] = Number(slider.value);
      panel.querySelector(`#out-${slider.dataset.weight}`).textContent = slider.value;
      render();
    });
  });
  panel.querySelector("#btn-save-weights").addEventListener("click", async () => {
    await api("/api/weights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ weights: state.weights }),
    });
    setStatus("weights saved to config.yaml", 1);
  });
}

/* ------------------------------------------------------------------ wiring */

function init() {
  $("#btn-listings").addEventListener("click", () =>
    startRun(runPayload({ search: true, details: false, routing: false }), "listings"));
  $("#btn-details").addEventListener("click", () =>
    startRun(runPayload({ search: false, details: true, routing: false }), "details"));
  $("#btn-commutes").addEventListener("click", () =>
    startRun(runPayload({ search: false, details: false, routing: true }), "commutes"));
  $("#btn-full").addEventListener("click", () =>
    startRun(runPayload({ search: true, details: true, routing: true }), "full run"));
  $("#btn-cancel").addEventListener("click", () => api("/api/jobs/cancel", { method: "POST" }));
  $("#btn-reload").addEventListener("click", loadListings);
  $("#btn-purge").addEventListener("click", async () => {
    const r = await api("/api/cache?kind=http", { method: "DELETE" });
    setStatus(`purged ${r.removed} cached pages`, 1);
    loadCacheStats();
  });
  $("#btn-weights").addEventListener("click", () => {
    const panel = $("#weights-panel");
    panel.hidden = !panel.hidden;
  });
  const menu = $("#profile-menu");
  $("#profile-btn").addEventListener("click", (ev) => {
    ev.stopPropagation();
    menu.hidden = !menu.hidden;
    $("#profile-btn").setAttribute("aria-expanded", String(!menu.hidden));
  });
  menu.addEventListener("click", (ev) => ev.stopPropagation());
  document.addEventListener("click", () => {
    menu.hidden = true;
    $("#profile-btn").setAttribute("aria-expanded", "false");
  });
  $$(".profile-opt").forEach((box) =>
    box.addEventListener("change", () => {
      renderProfileButton();
      loadListings();
    }));
  const setAll = (checked) => {
    $$(".profile-opt").forEach((box) => (box.checked = checked));
    renderProfileButton();
    loadListings();
  };
  $("#profile-all").addEventListener("click", () => setAll(true));
  $("#profile-none").addEventListener("click", () => setAll(false));
  renderProfileButton();

  $$(".filters input").forEach((input) =>
    input.addEventListener("input", render));
  $$("th[data-sort]").forEach((th) =>
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sort = state.sort.key === key
        ? { key, dir: -state.sort.dir }
        : { key, dir: key === "title" || key === "city_part" || key === "disposition" ? 1 : -1 };
      // (date sorts newest-first by default, like every other numeric column)
      render();
    }));

  loadListings();
  pollJob();
}

document.addEventListener("DOMContentLoaded", init);
