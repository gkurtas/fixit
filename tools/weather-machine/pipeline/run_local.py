#!/usr/bin/env python3
"""Run the Philadelphia Weather Machine rewrite pipeline on your own machine.

It fetches NWS alerts + IEM storm reports (or the bundled demo fixtures),
rewrites the prose into plain language with Claude, runs the fidelity guardrail,
and writes feed.json next to the web page. Open the page locally and it will
read that file.

  export ANTHROPIC_API_KEY=sk-ant-...
  python pipeline/run_local.py                 # live data for the PHI region
  python pipeline/run_local.py --source demo   # bundled fixtures (calm-day testing)
  python pipeline/run_local.py --limit 5       # only rewrite 5 new items (cheap test)
"""

import argparse
import os
import sys

import anthropic

from core import (
    REPO_ROOT,
    FileCache,
    assemble_feed,
    fetch_live,
    load_demo,
    resolve_watches,
    rewrite_items,
    write_feed,
)


def load_dotenv():
    """Read KEY=VALUE lines from a gitignored .env file in the project root, so
    the API key can live in a file (never on a command line or in shell history).
    A real environment variable always wins over the file."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Rewrite NWS alerts into plain language.")
    ap.add_argument("--source", choices=["live", "demo"], default="live",
                    help="live = real PHI-region data; demo = bundled fixtures")
    ap.add_argument("--out", default=str(REPO_ROOT / "feed.json"),
                    help="where to write the enriched feed (default: ./feed.json)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many NEW items to rewrite this run (keeps cost tiny)")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set your key first:  export ANTHROPIC_API_KEY=sk-ant-...")

    client = anthropic.Anthropic()
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    feed_alerts, map_alerts, watch_raw, reports = (
        load_demo() if args.source == "demo" else fetch_live()
    )
    cache = FileCache(REPO_ROOT / "pipeline" / ".cache" / "rewrites.json")
    watch_alerts = resolve_watches(watch_raw, cache) if watch_raw else []
    print(f"fetched {len(feed_alerts)} PHI alerts, {len(map_alerts)} neighbor map polygons, "
          f"{len(watch_alerts)} watch polygons, {len(reports)} storm reports ({args.source})")

    a = rewrite_items(client, model, feed_alerts, "alert", cache, args.limit)
    l = rewrite_items(client, model, reports, "lsr", cache, args.limit)
    cache.save()

    write_feed(assemble_feed(feed_alerts, reports, map_alerts, watch_alerts), args.out)

    print(f"wrote {args.out}")
    for name, s in (("alerts", a), ("storm reports", l)):
        print(f"  {name}: {s['rewritten']} rewritten, {s['cached']} cached, "
              f"{s['failed']} failed guardrail (shown as official text), {s['skipped']} skipped")


if __name__ == "__main__":
    main()
