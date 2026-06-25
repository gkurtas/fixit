"""Shared, dependency-free rewrite logic.

This module imports nothing outside the Python standard library, so it can be
used unchanged by BOTH:
  - the local runner (core.py), which calls Claude via the Anthropic SDK, and
  - the AWS Lambda (lambda_function.py), which calls Claude via Bedrock + boto3.

Keeping the prompt and the fidelity guardrail here means there is a single
source of truth for the safety-critical pieces.
"""

import hashlib
import re
from datetime import datetime, timezone

# --- Data sources -----------------------------------------------------------
# Query a wide enough area to capture Mount Holly (PHI) plus the four
# neighboring offices whose warning polygons we draw on the map. Text (the
# cards) stays PHI-only via SENDER; the map adds MAP_OFFICES.
ALERTS_URL = "https://api.weather.gov/alerts/active?area=PA,NJ,DE,MD,NY,CT,VA,WV,DC"
LSR_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.php?hours=24"
SENDER = "NWS Mount Holly NJ"
WFO = "PHI"

# Neighboring NWS offices whose warning POLYGONS appear on the map (no text /
# no rewrite). Exact senderName strings as they appear in the live feed —
# verified against api.weather.gov where possible (OKX is "Upton", not "New
# York"). Edit this set to add/remove offices.
MAP_OFFICES = {
    "NWS Upton NY",                    # OKX (New York)
    "NWS State College PA",            # CTP
    "NWS Binghamton NY",               # BGM
    "NWS Baltimore MD/Washington DC",  # LWX
}

# Output cap — well above what a 2-3 sentence rewrite needs.
MAX_TOKENS = 1024

SYSTEM_PROMPT = """You rewrite United States National Weather Service products into plain language for a public news audience. Your rewrites must stay faithful to the original — readers may make safety decisions based on them.

Follow these rules exactly:
- Stay faithful to the source. Do not exaggerate, downplay, editorialize, or add facts beyond what these rules allow.
- Keep every number, measurement, time, speed, direction, and place name exactly as written. Do not change, convert, or round a numeric value.
- If the source spells a number out as a word (for example "one foot" or "one half foot"), keep it as a word. Do not convert spelled-out numbers into digits.
- Never introduce a number, time, or place that is not in the source text. If a figure is not stated, do not state one.
- Do not calculate, total, or combine numbers. For example, if the source lists county counts per state, do not add them into an overall total — just describe the areas without a derived number.
- You may add a conventional unit to a bare measurement when the unit is standard for that kind of report (for example, a marine wind speed is reported in knots). Never change the number itself.
- Only simplify language: expand abbreviations (for example "TSTM WND GST" becomes "thunderstorm wind gust"), unpack jargon, and use short, clear sentences.
- If you are unsure what something means, keep the original wording rather than guessing.
- Include the location, the time, and the storm's motion in your summary when the source gives them — they help the reader. Do not mention the issuing office.
- Write calm, factual sentences in sentence case. No hype, no emoji, no opinions.

Return two fields:
- plain_headline: one clear line, 12 words or fewer.
- plain_summary: one to three short sentences a general reader can understand."""

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# A colon between digits is a reformatted clock time ("605 PM" -> "6:05 PM");
# collapse it so the time reads as one number again before comparing.
_TIME_COLON = re.compile(r"(?<=\d):(?=\d)")


def item_id(feature, kind):
    """Stable identity for caching and dedup.

    Alerts carry a stable URN id. IEM storm reports, however, only carry a
    response-relative index ('0','1','2'…) that shifts as the rolling 24h window
    changes — relying on it made the Slack digest re-report the same reports (and
    re-run their rewrites). So for storm reports we derive a stable id from the
    report's own content (time, location, type, magnitude, remark)."""
    p = feature.get("properties") or {}
    if kind == "alert":
        return feature.get("id") or p.get("id") or ""
    g = feature.get("geometry") or {}
    coords = g.get("coordinates") or []
    lon = coords[0] if len(coords) > 0 else ""
    lat = coords[1] if len(coords) > 1 else ""
    basis = "|".join(str(x) for x in [
        p.get("valid"), lat, lon, p.get("typetext"), p.get("magnitude"),
        p.get("source"), p.get("city"), p.get("remark"),
    ])
    return "lsr-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def source_parts(feature, kind):
    """The original prose handed to the model. Structured facts (location,
    times, office, severity) are excluded — the page shows those from the raw
    feed, so the model never has the chance to alter them."""
    p = feature.get("properties") or {}
    if kind == "alert":
        ctx = ("This is an active National Weather Service alert. The official "
               "event name is shown to readers as the headline, so put your effort "
               "into plain_summary. For plain_headline, simply restate the official "
               "event name in plain words.")
        fields = [
            ("Event", p.get("event")),
            ("Headline", p.get("headline")),
            ("Description", p.get("description")),
            ("Instruction", p.get("instruction")),
        ]
    else:
        ctx = ("This is a local storm report describing something already observed "
               "in the field. You may rewrite both the headline and the summary for "
               "clarity.")
        fields = [
            ("Type", p.get("typetext")),
            ("Magnitude", p.get("magnitude")),
            ("Remark", p.get("remark")),
        ]
    body = "\n".join(f"{k}: {v}" for k, v in fields if v)
    return ctx, body


def _numbers(text):
    """Numeric values in the text, as floats — so formatting differences don't
    trip the guardrail. Clock-time colons are collapsed ("6:05" -> 605) and
    decimals normalize (1 == 1.00), while a genuinely different value (80 vs 60)
    still stands out."""
    cleaned = _TIME_COLON.sub("", text or "")
    out = set()
    for m in _NUM_RE.findall(cleaned):
        try:
            out.add(float(m))
        except ValueError:
            pass
    return out


def passes_guardrail(source_text, headline, summary):
    """The hard safety check: the rewrite may not contain any number absent from
    the source (a fabricated or altered figure). Dropping a number is allowed
    (the official text is always one tap away); inventing one is not. Also
    rejects empty or runaway output."""
    head = (headline or "").strip()
    summ = (summary or "").strip()
    if not head or not summ:
        return False
    if len(head) > 140 or len(summ) > 600:
        return False
    invented = _numbers(head + " " + summ) - _numbers(source_text)
    return not invented


def _has_polygon(feature):
    g = feature.get("geometry") or {}
    return g.get("type") in ("Polygon", "MultiPolygon")


def trim_map_alert(feature):
    """Keep only what the map needs — geometry + a few properties — for a
    neighboring-office polygon. No description/instruction (no rewrite), to keep
    feed.json small. `expires`/`ends` let the page age the polygon out."""
    p = feature.get("properties") or {}
    return {
        "type": "Feature",
        "id": feature.get("id"),
        "geometry": feature.get("geometry"),
        "properties": {
            "event": p.get("event"),
            "severity": p.get("severity"),
            "areaDesc": p.get("areaDesc"),
            "senderName": p.get("senderName"),
            "expires": p.get("expires"),
            "ends": p.get("ends"),
        },
    }


def select_map_alerts(all_features):
    """From the broad alert set, pick the neighboring offices' polygon warnings
    for the map (PHI's own polygons travel with the feed alerts, so they're
    excluded here to avoid drawing them twice).

    Returns (trimmed_features, unmatched) where `unmatched` maps senderName ->
    count of polygon warnings from offices we did NOT include — a cheap signal
    for catching a wrong/missing office string (e.g. if CTP or BGM ever differ
    from the expected name)."""
    out = []
    unmatched = {}
    for f in all_features:
        if not _has_polygon(f):
            continue
        sn = (f.get("properties") or {}).get("senderName", "")
        if sn in MAP_OFFICES:
            out.append(trim_map_alert(f))
        elif sn != SENDER:
            unmatched[sn] = unmatched.get(sn, 0) + 1
    return out, unmatched


# --- Watches (zone-based: resolve the affected zones to polygons) ------------
# Watches arrive with geometry:null and reference county/forecast zones. We draw
# them region-wide (the same offices as the map polygons, plus PHI) as a subtle
# layer beneath the warnings. Zone boundaries are static, so the caller caches
# them; here we just hold the dependency-free logic.

WATCH_SOURCES = {SENDER} | MAP_OFFICES


def is_watch(feature):
    ev = ((feature.get("properties") or {}).get("event") or "")
    return ev.strip().endswith("Watch")


def select_watch_alerts(all_features):
    """Raw watch features (still carrying affectedZones) from our offices."""
    return [
        f for f in all_features
        if is_watch(f) and (f.get("properties") or {}).get("senderName") in WATCH_SOURCES
    ]


def _dedup_ring(ring):
    out = []
    for pt in ring:
        if not out or out[-1] != pt:
            out.append(pt)
    if len(out) >= 2 and out[0] != out[-1]:
        out.append(out[0])  # keep the ring closed
    return out


def simplify_geometry(geom, precision=2):
    """Round coordinates (watch areas are large; ~1 km precision is plenty) and
    drop consecutive duplicates. Pure Python — keeps feed.json small without a
    geometry library. ~2 decimals shrinks a county zone from ~30 KB to a few KB."""
    if not geom:
        return None
    t = geom.get("type")
    r = lambda v: round(v, precision)
    def ring(rg):
        return _dedup_ring([[r(a), r(b)] for a, b in rg])
    if t == "Polygon":
        return {"type": "Polygon", "coordinates": [ring(rg) for rg in geom["coordinates"]]}
    if t == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[ring(rg) for rg in poly] for poly in geom["coordinates"]]}
    return geom


def merge_polygons(geoms):
    """Combine several zone polygons into one MultiPolygon for a single watch."""
    coords = []
    for g in geoms:
        if not g:
            continue
        if g.get("type") == "Polygon":
            coords.append(g["coordinates"])
        elif g.get("type") == "MultiPolygon":
            coords.extend(g["coordinates"])
    return {"type": "MultiPolygon", "coordinates": coords} if coords else None


def trim_watch_alert(feature, geometry):
    p = feature.get("properties") or {}
    return {
        "type": "Feature",
        "id": feature.get("id"),
        "geometry": geometry,
        "properties": {
            "event": p.get("event"),
            "severity": p.get("severity"),
            "areaDesc": p.get("areaDesc"),
            "senderName": p.get("senderName"),
            "expires": p.get("expires"),
            "ends": p.get("ends"),
        },
    }


def resolve_watches(watch_features, get_zone, put_zone, fetch_geom,
                    max_zones=60, max_fetches=80):
    """Turn raw watch alerts into map-ready features with simplified geometry.

    IO is injected so this stays dependency-free and shared:
      get_zone(zone_id)        -> cached simplified geometry or None
      put_zone(zone_id, geom)  -> store simplified geometry (caller sets TTL)
      fetch_geom(zone_url)     -> raw geometry dict from the NWS zone endpoint
    max_fetches bounds new zone downloads per run; uncached overflow resolves on
    a later run (zones are cached forever-ish, so this only bites the first time)."""
    out = []
    fetches = 0
    for f in watch_features:
        # If the watch already has a polygon, use it directly.
        g0 = f.get("geometry")
        if g0 and g0.get("type") in ("Polygon", "MultiPolygon"):
            out.append(trim_watch_alert(f, simplify_geometry(g0)))
            continue
        geoms = []
        for url in ((f.get("properties") or {}).get("affectedZones") or [])[:max_zones]:
            zid = url.rsplit("/", 1)[-1]
            g = get_zone(zid)
            if g is None and fetches < max_fetches:
                raw = fetch_geom(url)
                fetches += 1
                if raw:
                    g = simplify_geometry(raw)
                    put_zone(zid, g)
            if g:
                geoms.append(g)
        mp = merge_polygons(geoms)
        if mp:
            out.append(trim_watch_alert(f, mp))
    return out


# --- Slack alerting ----------------------------------------------------------
# Post to Slack when one of these warning types affects one of these counties.
# Counties matched by NWS UGC code (verified against the zone API). Edit freely.
SLACK_EVENTS = {
    "Tornado Warning",
    "Flash Flood Warning",
    "Severe Thunderstorm Warning",
}
SLACK_COUNTIES = {
    "PAC017": "Bucks (PA)",
    "PAC029": "Chester (PA)",
    "PAC045": "Delaware (PA)",
    "PAC091": "Montgomery (PA)",
    "PAC101": "Philadelphia (PA)",
    "NJC005": "Burlington (NJ)",
    "NJC007": "Camden (NJ)",
    "NJC015": "Gloucester (NJ)",
}

# The storm-report digest is scoped to the same counties as the Slack warning
# alerts (derived from SLACK_COUNTIES so the two can't drift). Reports carry a
# county NAME + state rather than the UGC code warnings use, so match on that.
def _county_key(label):  # "Bucks (PA)" -> ("BUCKS", "PA")
    name, _, st = label.partition("(")
    return name.strip().upper(), st.strip().strip(")").strip().upper()


DIGEST_COUNTIES = {_county_key(v) for v in SLACK_COUNTIES.values()}


def report_in_scope(feature):
    """True if a storm report is in one of the digest's target counties."""
    p = feature.get("properties") or {}
    county = (p.get("county") or "").strip().upper()
    st = (p.get("st") or p.get("state") or "").strip().upper()
    return (county, st) in DIGEST_COUNTIES


# Severity bar colors for Slack — keep in sync with config.js severityColor.
SEVERITY_COLOR = {
    "Extreme": "#6a1b9a",
    "Severe": "#c62828",
    "Moderate": "#d97706",
    "Minor": "#1565c0",
    "Unknown": "#546e7a",
}


# Storm-report categories (server-side mirror of config.js lsrCategories),
# used to group reports in the periodic digest.
LSR_CATEGORIES = [
    ("Tornado", ["TORNADO", "FUNNEL", "WATERSPOUT"]),
    ("Flooding", ["FLOOD", "FLASH FLOOD"]),
    ("Hail", ["HAIL"]),
    ("Wind", ["TSTM WND", "WIND", "GST", "GUST", "MARINE"]),
    ("Snow/Ice", ["SNOW", "ICE", "SLEET", "FREEZING"]),
    ("Rain", ["RAIN", "PRECIP"]),
]


def lsr_category(typetext):
    t = (typetext or "").upper()
    for label, kws in LSR_CATEGORIES:
        if any(k in t for k in kws):
            return label
    return "Other"


def lead_within_facts(facts_text, lead):
    """Digest guardrail: the AI lead sentence may only use numbers that appear in
    the computed facts (no invented counts or magnitudes)."""
    if not (lead or "").strip():
        return False
    return _numbers(lead).issubset(_numbers(facts_text))


def slack_match(feature):
    """If this alert is a watched warning type affecting a watched county,
    return the matched county names; otherwise an empty list."""
    p = feature.get("properties") or {}
    if (p.get("event") or "") not in SLACK_EVENTS:
        return []
    ugc = ((p.get("geocode") or {}).get("UGC")) or []
    return [SLACK_COUNTIES[c] for c in ugc if c in SLACK_COUNTIES]


def assemble_feed(alerts, reports, map_alerts=None, watch_alerts=None):
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": {"type": "FeatureCollection", "features": alerts},
        "lsr": {"type": "FeatureCollection", "features": reports},
        "map_alerts": {"type": "FeatureCollection", "features": map_alerts or []},
        "watch_alerts": {"type": "FeatureCollection", "features": watch_alerts or []},
    }
