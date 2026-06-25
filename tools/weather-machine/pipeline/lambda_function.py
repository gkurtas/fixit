"""AWS Lambda: fetch NWS/IEM data, rewrite via Bedrock, write feed.json to S3.

Runs on an EventBridge schedule. Uses only boto3 + the standard library (both
present in the Lambda runtime), so there are no dependencies to package. The
rewrite logic, prompt, and guardrail are shared with the local runner via
rewrite_common.py.

Environment variables (set by the CloudFormation stack):
  BEDROCK_MODEL_ID   e.g. us.anthropic.claude-sonnet-4-6
  CACHE_TABLE        DynamoDB table name (rewrite cache)
  SITE_BUCKET        S3 bucket to write feed.json into
  FEED_KEY           object key for the feed (default: feed.json)
  CONTACT_EMAIL      shown to NWS in the request User-Agent
  MAX_NEW_PER_RUN    cap on new model calls per invocation (cost guardrail)
  SLACK_SECRET_ID    Secrets Manager secret id/ARN holding the Slack webhook URL
"""

import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime

import boto3
from botocore.config import Config

from rewrite_common import (
    ALERTS_URL,
    LSR_URL,
    MAX_TOKENS,
    SENDER,
    SYSTEM_PROMPT,
    WFO,
    LSR_CATEGORIES,
    SEVERITY_COLOR,
    assemble_feed,
    item_id,
    lead_within_facts,
    lsr_category,
    passes_guardrail,
    report_in_scope,
    resolve_watches,
    select_map_alerts,
    select_watch_alerts,
    slack_match,
    source_parts,
)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
CACHE_TABLE = os.environ["CACHE_TABLE"]
SITE_BUCKET = os.environ["SITE_BUCKET"]
FEED_KEY = os.environ.get("FEED_KEY", "feed.json")
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "60"))
SLACK_SECRET_ID = os.environ.get("SLACK_SECRET_ID", "")
SITE_URL = os.environ.get("SITE_URL", "")
DIGEST_INTERVAL_SEC = int(os.environ.get("DIGEST_INTERVAL_MIN", "30")) * 60
DIGEST_BURST = int(os.environ.get("DIGEST_BURST", "12"))  # flush early if this many pile up
DIGEST_COLOR = "#455a64"  # slate — visually distinct from the severity-colored warnings

_bedrock = boto3.client(
    "bedrock-runtime", region_name=REGION,
    config=Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=60),
)
_ddb = boto3.client("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
_secrets = boto3.client("secretsmanager", region_name=REGION)

# Forcing this tool makes the model return exactly the two fields — the Bedrock
# equivalent of the structured output we used locally.
REWRITE_TOOL = {
    "toolSpec": {
        "name": "emit_rewrite",
        "description": "Return the plain-language rewrite of the weather product.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "plain_headline": {"type": "string",
                                   "description": "Clear one-line headline, sentence case, 12 words or fewer."},
                "plain_summary": {"type": "string",
                                  "description": "One to three short plain-language sentences."},
            },
            "required": ["plain_headline", "plain_summary"],
        }},
    }
}


# --- Fetch (stdlib urllib — no requests dependency) --------------------------

def _fetch_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"philadelphia-weather-machine ({os.environ.get('CONTACT_EMAIL', 'weather-machine@example.com')})",
        "Accept": "application/geo+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_live():
    alerts_all = _fetch_json(ALERTS_URL).get("features", [])
    feed_alerts = [
        f for f in alerts_all
        if (f.get("properties") or {}).get("senderName") == SENDER
    ]
    map_alerts, unmatched = select_map_alerts(alerts_all)
    if unmatched:
        print("map: polygon warnings from offices not shown:", json.dumps(unmatched))
    watch_raw = select_watch_alerts(alerts_all)
    reports = [
        f for f in _fetch_json(LSR_URL).get("features", [])
        if (f.get("properties") or {}).get("wfo") == WFO
    ]
    return feed_alerts, map_alerts, watch_raw, reports


# --- Zone-boundary cache (static shapes; long TTL, same DynamoDB table) -------

ZONE_TTL = 90 * 24 * 3600


def zone_get(zid):
    r = _ddb.get_item(TableName=CACHE_TABLE, Key={"id": {"S": "zone:" + zid}},
                      ProjectionExpression="payload")
    item = r.get("Item")
    return json.loads(item["payload"]["S"]) if item else None


def zone_put(zid, geom):
    _ddb.put_item(TableName=CACHE_TABLE, Item={
        "id": {"S": "zone:" + zid},
        "payload": {"S": json.dumps(geom)},
        "ttl": {"N": str(int(time.time()) + ZONE_TTL)},
    })


def fetch_zone_geom(url):
    try:
        return (_fetch_json(url) or {}).get("geometry")
    except Exception as e:  # noqa: BLE001
        print("zone fetch failed:", url, repr(e))
        return None


# --- Rewrite (Bedrock Converse) ----------------------------------------------

def rewrite_one(ctx, body):
    """Returns (headline, summary), or (None, None) if no tool output came back."""
    resp = _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": f"{ctx}\n\n{body}"}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS},
        toolConfig={"tools": [REWRITE_TOOL], "toolChoice": {"tool": {"name": "emit_rewrite"}}},
    )
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            inp = block["toolUse"]["input"]
            return inp.get("plain_headline", ""), inp.get("plain_summary", "")
    return None, None


# --- Cache (DynamoDB) --------------------------------------------------------

def cache_get(key):
    r = _ddb.get_item(TableName=CACHE_TABLE, Key={"id": {"S": key}},
                      ProjectionExpression="payload")
    item = r.get("Item")
    return json.loads(item["payload"]["S"]) if item else None


def cache_put(key, plain, ttl_epoch):
    item = {"id": {"S": key}, "payload": {"S": json.dumps(plain)}}
    if ttl_epoch:
        item["ttl"] = {"N": str(int(ttl_epoch))}
    _ddb.put_item(TableName=CACHE_TABLE, Item=item)


def _ttl_epoch(feature, kind):
    """Auto-expire cache rows: alerts an hour past their end time, reports +2d."""
    p = feature.get("properties") or {}
    if kind == "alert":
        t = p.get("ends") or p.get("expires")
        if t:
            try:
                return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()) + 3600
            except Exception:  # noqa: BLE001
                pass
    return int(time.time()) + 2 * 24 * 3600


# --- Process -----------------------------------------------------------------

def process(features, kind):
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
        cached = cache_get(key)
        if cached is not None:
            p["plain"] = cached
            stats["cached"] += 1
            continue

        if new_calls >= MAX_NEW_PER_RUN:
            p["plain"] = {"verified": False, "ai": False, "skipped": True}
            stats["skipped"] += 1
            continue

        errored = False
        try:
            h, s = rewrite_one(ctx, body)
            if not h:
                raise ValueError("no tool output returned")
            ok = passes_guardrail(body, h, s)
            plain = {"headline": h.strip(), "summary": s.strip(), "verified": bool(ok), "ai": True}
        except Exception as e:  # noqa: BLE001
            print(f"rewrite failed for {item_id(f, kind)}: {e!r}")
            plain = {"verified": False, "ai": False, "error": True}
            ok = False
            errored = True

        p["plain"] = plain
        # Only cache real model results (verified or guardrail-failed). Never
        # cache a transient error (throttling, access not yet granted) — leaving
        # it uncached means the next scheduled run retries instead of getting stuck.
        if not errored:
            cache_put(key, plain, _ttl_epoch(f, kind))
        new_calls += 1
        stats["rewritten" if ok else "failed"] += 1

    return stats


# --- Slack alerts ------------------------------------------------------------

_slack_url = None


def slack_webhook():
    """Read the webhook URL from Secrets Manager (cached once it's available).
    Until the secret holds a value, returns "" and we simply skip Slack (no
    crash). The secret stores the raw webhook URL as its SecretString."""
    global _slack_url
    if _slack_url:
        return _slack_url
    if not SLACK_SECRET_ID:
        return ""
    try:
        _slack_url = (_secrets.get_secret_value(SecretId=SLACK_SECRET_ID)["SecretString"] or "").strip()
        return _slack_url
    except Exception as e:  # noqa: BLE001
        print("slack: webhook not available yet:", repr(e))
        return ""


def slack_post(payload):
    url = slack_webhook()
    if not url:
        return False
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print("slack post failed:", repr(e))
        return False


def _fmt_when(iso):
    if not iso:
        return "further notice"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%-I:%M %p ET")
    except Exception:  # noqa: BLE001
        return iso


def _slack_already_sent(alert_id):
    r = _ddb.get_item(TableName=CACHE_TABLE, Key={"id": {"S": "slack:" + alert_id}},
                      ProjectionExpression="id")
    return "Item" in r


def _slack_mark_sent(alert_id, ttl_epoch):
    _ddb.put_item(TableName=CACHE_TABLE, Item={
        "id": {"S": "slack:" + alert_id}, "ttl": {"N": str(int(ttl_epoch))}})


def _slack_message(event_name, severity, counties, when, summary):
    lines = [
        f"*{event_name}*  ·  _{severity}_",
        f":round_pushpin: {', '.join(sorted(set(counties)))}",
        f":clock3: Until {when}",
    ]
    if summary:
        lines.append(summary)
    if SITE_URL:
        lines.append(f"<{SITE_URL}|View the live feed>")
    return {
        "attachments": [{
            "color": SEVERITY_COLOR.get(severity, SEVERITY_COLOR["Unknown"]),
            "fallback": f"{event_name} ({severity}) — {', '.join(counties)}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}],
        }]
    }


def maybe_slack(feed_alerts):
    """Post once per matching alert (event type x county). Dedup via DynamoDB."""
    sent = 0
    for f in feed_alerts:
        counties = slack_match(f)
        if not counties:
            continue
        aid = item_id(f, "alert")
        if not aid or _slack_already_sent(aid):
            continue
        p = f.get("properties") or {}
        sev = p.get("severity") or "Unknown"
        plain = p.get("plain") or {}
        summary = plain.get("summary", "") if plain.get("verified") else ""
        msg = _slack_message(p.get("event", "Alert"), sev, counties,
                             _fmt_when(p.get("expires") or p.get("ends")), summary)
        if slack_post(msg):
            _slack_mark_sent(aid, _ttl_epoch(f, "alert") + 6 * 3600)
            sent += 1
    return sent


# --- Storm-report digest -----------------------------------------------------
# Storm reports are high-volume, so we don't post each one. Instead we batch new
# reports and post a periodic digest — at most every DIGEST_INTERVAL, with an
# early flush if a burst piles up — and stay silent when nothing new arrives.

DIGEST_SYSTEM = (
    "You write a brief, calm, plain-language summary of local storm reports for a "
    "public weather channel. One to three sentences is ideal, but a little longer "
    "is fine when it delivers pertinent detail. Describe what happened — the kinds "
    "of damage and the peak measurements — and name the specific locations "
    "(municipalities, towns, or streets) whenever the reports provide them; that "
    "granular 'where' is the most useful part. Do not merely repeat the county "
    "list shown separately above your text; add the finer detail. Use only the "
    "facts, numbers, and place names given to you; never add, change, or invent a "
    "number or a place. No hype, no emoji."
)


def _digest_seen(rid):
    r = _ddb.get_item(TableName=CACHE_TABLE, Key={"id": {"S": "lsrdigest:" + rid}},
                      ProjectionExpression="id")
    return "Item" in r


def _digest_mark(rid):
    _ddb.put_item(TableName=CACHE_TABLE, Item={
        "id": {"S": "lsrdigest:" + rid}, "ttl": {"N": str(int(time.time()) + 2 * 24 * 3600)}})


def _digest_last_at():
    r = _ddb.get_item(TableName=CACHE_TABLE, Key={"id": {"S": "lsrdigest:state"}},
                      ProjectionExpression="payload")
    it = r.get("Item")
    if not it:
        return 0.0
    try:
        return float(json.loads(it["payload"]["S"]).get("last_at", 0))
    except Exception:  # noqa: BLE001
        return 0.0


def _digest_set_last_at(ts):
    _ddb.put_item(TableName=CACHE_TABLE, Item={
        "id": {"S": "lsrdigest:state"}, "payload": {"S": json.dumps({"last_at": ts})}})


def _num(x):
    try:
        return float(str(x))
    except Exception:  # noqa: BLE001
        return None


def _digest_stats(reports):
    """Compute exact, code-derived facts about a batch of reports, including a
    per-report detail list (location + what happened) so the model can cite
    specific municipalities and streets."""
    from collections import Counter, OrderedDict
    cats = Counter()
    counties = OrderedDict()
    max_wind = (None, "")  # (value, unit)
    max_hail = None
    details = []
    for r in reports:
        p = r.get("properties") or {}
        cat = lsr_category(p.get("typetext"))
        cats[cat] += 1
        county = (p.get("county") or "").strip()
        if county:
            counties[county] = True
        mag = _num(p.get("magnitude"))
        if mag is not None and cat == "Wind" and (max_wind[0] is None or mag > max_wind[0]):
            max_wind = (mag, (p.get("unit") or "mph"))
        if mag is not None and cat == "Hail" and (max_hail is None or mag > max_hail):
            max_hail = mag
        if len(details) < 18:  # cap to bound tokens; aggregate counts cover the rest
            city = (p.get("city") or "").strip()
            where = ", ".join([x for x in (city, county) if x]) or "an unspecified location"
            rm = (p.get("remark") or p.get("typetext") or "").strip()
            unit = (p.get("unit") or "").strip()
            mtxt = (f" ({mag:g} {unit})" if unit else f" ({mag:g})") if mag is not None else ""
            details.append(f"- {cat} near {where}: {rm}{mtxt}")
    cat_order = [lbl for lbl, _ in LSR_CATEGORIES] + ["Other"]
    return {
        "total": len(reports),
        "by_cat": [(lbl, cats[lbl]) for lbl in cat_order if cats.get(lbl)],
        "counties": list(counties.keys()),
        "max_wind": max_wind,
        "max_hail": max_hail,
        "details": details,
    }


def _digest_facts_text(stats, mins):
    parts = [f"{stats['total']} new local storm reports in the past {mins} minutes."]
    if stats["by_cat"]:
        parts.append("By type: " + ", ".join(f"{lbl} {n}" for lbl, n in stats["by_cat"]) + ".")
    if stats["max_wind"][0] is not None:
        v = stats["max_wind"][0]
        parts.append(f"Peak wind gust: {v:g} {stats['max_wind'][1]}.")
    if stats["max_hail"] is not None:
        parts.append(f"Largest hail: {stats['max_hail']:g} in.")
    if stats["counties"]:
        parts.append("Counties: " + ", ".join(stats["counties"][:8]) + ".")
    if stats["details"]:
        parts.append("Individual reports (use these for specific locations):")
        parts.extend(stats["details"])
    return "\n".join(parts)


def digest_lead(facts_text):
    try:
        resp = _bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": DIGEST_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": facts_text}]}],
            inferenceConfig={"maxTokens": 400},
        )
        for b in resp["output"]["message"]["content"]:
            if "text" in b:
                return b["text"].strip()
    except Exception as e:  # noqa: BLE001
        print("digest lead failed:", repr(e))
    return ""


def _digest_payload(stats, lead, mins):
    bits = []
    if stats["by_cat"]:
        bits.append("  ".join(f"{lbl} {n}" for lbl, n in stats["by_cat"]))
    peaks = []
    if stats["max_wind"][0] is not None:
        peaks.append(f"peak gust {stats['max_wind'][0]:g} {stats['max_wind'][1]}")
    if stats["max_hail"] is not None:
        peaks.append(f"hail {stats['max_hail']:g} in")
    lines = [f"*Storm reports — past {mins} min*  ·  {stats['total']} new"]
    if stats["counties"]:
        lines.append("*Counties:* " + ", ".join(stats["counties"][:8]))
    lines.append(lead)
    if bits:
        lines.append("  ·  ".join(filter(None, [" · ".join(bits), ", ".join(peaks)])))
    if SITE_URL:
        lines.append(f"<{SITE_URL}|View the live feed>")
    return {"attachments": [{
        "color": DIGEST_COLOR,
        "fallback": f"{stats['total']} new storm reports",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}],
    }]}


def maybe_report_digest(reports):
    in_scope = [r for r in reports if report_in_scope(r)]
    pending = [r for r in in_scope if not _digest_seen(item_id(r, "lsr"))]
    last_at = _digest_last_at()
    now = time.time()

    # First ever run: adopt the current backlog as "seen" and start the clock,
    # so we don't dump 24 hours of reports as one giant first digest.
    if last_at == 0:
        for r in pending:
            _digest_mark(item_id(r, "lsr"))
        _digest_set_last_at(now)
        return 0

    if not pending:
        return 0
    elapsed = now - last_at
    if len(pending) < DIGEST_BURST and elapsed < DIGEST_INTERVAL_SEC:
        return 0  # hold and keep accumulating

    mins = max(1, round(elapsed / 60))
    stats = _digest_stats(pending)
    facts_text = _digest_facts_text(stats, mins)
    lead = digest_lead(facts_text)
    if not lead or not lead_within_facts(facts_text, lead):
        lead = f"{stats['total']} new local storm reports in the past {mins} minutes."
    if slack_post(_digest_payload(stats, lead, mins)):
        for r in pending:
            _digest_mark(item_id(r, "lsr"))
        _digest_set_last_at(now)
        return len(pending)
    return 0


def handler(event, context):
    # Test hook: `aws lambda invoke ... --payload '{"slack_test": true}'`
    if isinstance(event, dict) and event.get("slack_test"):
        ok = slack_post(_slack_message(
            "Severe Thunderstorm Warning", "Severe", ["Philadelphia (PA)"],
            "8:45 PM ET", "Test message from the weather pipeline — Slack is wired up correctly."))
        print(json.dumps({"slack_test": ok}))
        return {"slack_test": ok}

    # Test hook for the report digest: `--payload '{"digest_test": true}'`
    if isinstance(event, dict) and event.get("digest_test"):
        sample = [
            {"properties": {"typetext": "TSTM WND DMG", "city": "Doylestown", "county": "Bucks",
                            "st": "PA", "magnitude": "58", "unit": "mph",
                            "remark": "Several trees and wires down along N Main St and W Court St."}},
            {"properties": {"typetext": "TSTM WND GST", "city": "Lansdale", "county": "Montgomery",
                            "st": "PA", "magnitude": "61", "unit": "mph",
                            "remark": "Measured gust at a weather station near Forty Foot Rd."}},
            {"properties": {"typetext": "HAIL", "city": "Cherry Hill", "county": "Camden",
                            "st": "NJ", "magnitude": "1.00",
                            "remark": "Quarter size hail covering the ground near Route 70."}},
        ]
        stats = _digest_stats(sample)
        facts = _digest_facts_text(stats, 28)
        lead = digest_lead(facts)
        if not lead or not lead_within_facts(facts, lead):
            lead = f"{stats['total']} new local storm reports in the past 28 minutes."
        ok = slack_post(_digest_payload(stats, lead, 28))
        print(json.dumps({"digest_test": ok, "lead": lead}))
        return {"digest_test": ok, "lead": lead}

    feed_alerts, map_alerts, watch_raw, reports = fetch_live()
    a = process(feed_alerts, "alert")  # PHI only — these get rewritten
    l = process(reports, "lsr")
    # map_alerts (neighbors' warnings) and watch_alerts are NOT rewritten — map only.
    watch_alerts = resolve_watches(watch_raw, zone_get, zone_put, fetch_zone_geom)
    # Slack runs after rewrites so the message can include the plain-language summary.
    slack_sent = maybe_slack(feed_alerts)
    digest_sent = maybe_report_digest(reports)

    feed = assemble_feed(feed_alerts, reports, map_alerts, watch_alerts)
    body = json.dumps(feed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _s3.put_object(
        Bucket=SITE_BUCKET, Key=FEED_KEY, Body=body,
        ContentType="application/json", CacheControl="public, max-age=60",
    )

    result = {"alerts": a, "reports": l, "map_polys": len(map_alerts),
              "watch_polys": len(watch_alerts), "slack_sent": slack_sent,
              "digest_sent": digest_sent, "feed_bytes": len(body)}
    print(json.dumps(result))
    return result
