"""Local rewrite pipeline — fetch, rewrite (Anthropic SDK), guardrail, feed.json.

The dependency-free logic (prompt, guardrail, source parsing, assembly) lives in
rewrite_common.py and is shared with the AWS Lambda. This file adds the local
input/output: HTTP fetch via requests, the demo fixtures, the Anthropic SDK
call, a file-backed cache, and writing feed.json to disk.
"""

import hashlib
import json
import os
from pathlib import Path

import requests

from prompt import Rewrite
from rewrite_common import (
    ALERTS_URL,
    LSR_URL,
    MAX_TOKENS,
    SENDER,
    SYSTEM_PROMPT,
    WFO,
    assemble_feed,
    item_id,
    passes_guardrail,
    resolve_watches as _resolve_watches,
    select_map_alerts,
    select_watch_alerts,
    source_parts,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Re-export so existing imports (run_local.py) keep working.
__all__ = [
    "REPO_ROOT", "FileCache", "fetch_live", "load_demo",
    "rewrite_items", "assemble_feed", "write_feed",
]


# --- Fetch -------------------------------------------------------------------

def _headers():
    contact = os.environ.get("CONTACT_EMAIL", "weather-machine@example.com")
    return {
        "User-Agent": f"philadelphia-weather-machine ({contact})",
        "Accept": "application/geo+json",
    }


def fetch_live():
    """Returns (feed_alerts, map_alerts, reports): PHI alerts for the cards,
    neighboring-office polygon warnings for the map, and PHI storm reports."""
    a = requests.get(ALERTS_URL, headers=_headers(), timeout=30)
    a.raise_for_status()
    alerts_all = a.json().get("features", [])
    feed_alerts = [
        f for f in alerts_all
        if (f.get("properties") or {}).get("senderName") == SENDER
    ]
    map_alerts, unmatched = select_map_alerts(alerts_all)
    if unmatched:
        print("  map: polygon warnings from offices not shown:", unmatched)
    watch_raw = select_watch_alerts(alerts_all)
    l = requests.get(LSR_URL, headers=_headers(), timeout=30)
    l.raise_for_status()
    reports = [
        f for f in l.json().get("features", [])
        if (f.get("properties") or {}).get("wfo") == WFO
    ]
    return feed_alerts, map_alerts, watch_raw, reports


def load_demo():
    """Bundled national fixtures — handy on a calm day. No office filter, to
    match the page's own demo mode."""
    alerts = json.loads((REPO_ROOT / "samples" / "sample-alerts.json").read_text()).get("features", [])
    reports = json.loads((REPO_ROOT / "samples" / "sample-lsr.json").read_text()).get("features", [])
    map_alerts, _ = select_map_alerts(alerts)
    return alerts, map_alerts, [], reports  # no watch resolution in demo


def resolve_watches(watch_raw, cache):
    """Resolve watch zone shapes locally (requests + the file cache)."""
    def fetch_geom(url):
        r = requests.get(url, headers=_headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("geometry")
    return _resolve_watches(
        watch_raw,
        lambda zid: cache.get("zone:" + zid),
        lambda zid, g: cache.set("zone:" + zid, g),
        fetch_geom,
    )


# --- Rewrite (Anthropic SDK) -------------------------------------------------

def rewrite_one(client, model, ctx, body):
    """One model call. Returns a validated Rewrite, or None on a refusal."""
    resp = client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"{ctx}\n\n{body}"}],
        output_format=Rewrite,
    )
    return resp.parsed_output


def rewrite_items(client, model, features, kind, cache, limit=None):
    """Annotate each feature in place with properties.plain, calling the model
    only for items not already cached (keyed by id + content hash)."""
    stats = {"rewritten": 0, "cached": 0, "failed": 0, "skipped": 0}
    new_calls = 0

    for f in features:
        p = f.setdefault("properties", {})
        ctx, body = source_parts(f, kind)

        if not body.strip():
            p["plain"] = {"verified": False, "ai": False}
            stats["skipped"] += 1
            continue

        key = f"{kind}:{item_id(f, kind)}:{hashlib.sha1(body.encode('utf-8')).hexdigest()[:10]}"
        cached = cache.get(key)
        if cached is not None:
            p["plain"] = cached
            stats["cached"] += 1
            continue

        if limit is not None and new_calls >= limit:
            p["plain"] = {"verified": False, "ai": False, "skipped": True}
            stats["skipped"] += 1
            continue

        errored = False
        try:
            r = rewrite_one(client, model, ctx, body)
            if r is None:
                raise ValueError("model returned no structured output (possible refusal)")
            ok = passes_guardrail(body, r.plain_headline, r.plain_summary)
            plain = {
                "headline": r.plain_headline.strip(),
                "summary": r.plain_summary.strip(),
                "verified": bool(ok),
                "ai": True,
            }
        except Exception as e:  # noqa: BLE001 — log and fall back to official text
            print(f"  ! rewrite failed for {item_id(f, kind)}: {e}")
            plain = {"verified": False, "ai": False, "error": True}
            ok = False
            errored = True

        p["plain"] = plain
        # Never cache a transient error — leave it uncached so the next run retries.
        if not errored:
            cache.set(key, plain)
        new_calls += 1
        stats["rewritten" if ok else "failed"] += 1

    return stats


# --- Write + cache (local) ---------------------------------------------------

def write_feed(feed, out_path):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")))


class FileCache:
    """Persists rewrites between local runs so re-running is free."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:  # noqa: BLE001 — a corrupt cache just starts fresh
                self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False))
