#!/usr/bin/env python3
"""
channel_subscriber_split.py — Live subscriber vs non-subscriber view split
for one deep-story channel (en | zh).

Called on-demand by story_engine's own API
(GET /api/analytics/channel/subscriber-split?upload_profile=en|zh)
and prints a single JSON object to stdout. Nothing is written to disk.

Sibling of games/db/subscriber_split.py (KataGo's version of this card) —
same output contract, same aggregation logic, different data sources:
  - video ids come from story_engine's own Postgres (`youtube_publish_log`,
    filtered by lang) instead of games.db (SQLite).
  - channel_id / token path come from ~/.config/pipe/youtube_profiles.json
    (keyed by "en"/"zh") instead of being hardcoded to the games channel.

The split is produced with a single YouTube Analytics query:

    dimensions = day,subscribedStatus
    filters    = video==<id1,id2,...>     (that channel's videos, <=500)
    metrics    = views

From the daily rows we derive both the 7/28/90-day KPI windows and the
weekly % trend, so one API call serves the whole card.

Auth: the token file named in youtube_profiles.json[lang]["token_path"].
      These tokens are already used for YouTube Analytics queries by
      pipe/code/deploy/youtube/fetch_analytics.py (channel audience /
      traffic sources), so yt-analytics.readonly is already granted.

Output contract (stdout, one JSON object) — identical shape to
games/db/subscriber_split.py's output:
    {
      "lang": "en",
      "channel_id": "UCPVH4BZZgKtIJHHdriEYsYw",
      "through": "2026-08-27",          # last day included (72h latency)
      "available": true,                # false => show a friendly notice
      "note": null,                     # reason string when available=false
      "video_count": 42,
      "windows": [ {key,label,subscribed,non_subscribed,total,pct}, ... ],
      "weeks":   [ {week_start,label,subscribed,non_subscribed,total,pct,partial}, ... ]
    }

Usage:
    python3 src/scripts/channel_subscriber_split.py --lang en
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# story_engine/src on sys.path so `from db.models import get_connection` works
# regardless of which venv this is invoked from (it's run under the `pipe`
# venv via subprocess — see api/routes.py — because that venv has both
# psycopg2 and the google-api-python-client libraries installed).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PROFILES_PATH = Path.home() / ".config" / "pipe" / "youtube_profiles.json"
_LATENCY_DAYS = 3          # YouTube Analytics is unstable within ~72h
_LOOKBACK_DAYS = 90        # KPI 90d window + weekly trend range
_MAX_FILTER_IDS = 500      # Analytics `video==` filter cap

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _emit(obj):
    """Print JSON and exit 0 (the backend treats non-zero exit as failure)."""
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    sys.exit(0)


def _unavailable(lang, note, channel_id=None, video_count=0, through=None):
    _emit({
        "lang": lang,
        "channel_id": channel_id,
        "through": through,
        "available": False,
        "note": note,
        "video_count": video_count,
        "windows": [],
        "weeks": [],
    })


def _week_label(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _video_ids_for_lang(lang: str) -> list:
    from db.models import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT video_id FROM youtube_publish_log "
            "WHERE video_id IS NOT NULL AND lang = %s",
            (lang,),
        ).fetchall()
    finally:
        conn.close()
    return [r["video_id"] if isinstance(r, dict) else r[0] for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True, choices=["en", "zh"])
    args = ap.parse_args()
    lang = args.lang

    if not _PROFILES_PATH.is_file():
        _unavailable(lang, f"youtube_profiles.json not found at {_PROFILES_PATH}.")

    profiles = json.load(open(_PROFILES_PATH, encoding="utf-8"))
    profile = profiles.get(lang)
    if not profile:
        _unavailable(lang, f"No '{lang}' entry in youtube_profiles.json.")

    channel_id = profile.get("channel_id")
    token_path = os.path.expanduser(profile.get("token_path", ""))

    try:
        video_ids = _video_ids_for_lang(lang)
    except Exception as e:
        _unavailable(lang, f"DB query failed: {e}", channel_id)
    if not video_ids:
        _unavailable(lang, f"No {lang} videos found in youtube_publish_log.", channel_id)

    end_d = date.today() - timedelta(days=_LATENCY_DAYS)
    start_d = end_d - timedelta(days=_LOOKBACK_DAYS - 1)
    through = end_d.isoformat()

    # ── auth + query ─────────────────────────────────────────────────────────
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        _unavailable(lang, f"google client not installed: {e}",
                     channel_id, len(video_ids), through)

    if not token_path or not os.path.isfile(token_path):
        _unavailable(lang, f"Token file not found: {token_path}",
                     channel_id, len(video_ids), through)

    try:
        tok = json.load(open(token_path))
        creds = Credentials.from_authorized_user_file(token_path, tok.get("scopes"))
        if not creds.valid:
            creds.refresh(Request())
        ya = build("youtubeAnalytics", "v2", credentials=creds,
                   cache_discovery=False)
    except Exception as e:
        _unavailable(lang, f"auth failed: {e}", channel_id, len(video_ids), through)

    filt = "video==" + ",".join(video_ids[:_MAX_FILTER_IDS])
    try:
        resp = ya.reports().query(
            ids=f"channel=={channel_id}",
            startDate=start_d.isoformat(),
            endDate=end_d.isoformat(),
            metrics="views",
            dimensions="day,subscribedStatus",
            filters=filt,
            sort="day",
        ).execute()
    except Exception as e:
        # YouTube returns HTTP 500 when it suppresses low-volume splits.
        msg = str(e)
        if "500" in msg or "internalError" in msg:
            _unavailable(
                lang,
                "YouTube is withholding the subscriber breakdown for this "
                "channel (too few views to disaggregate yet).",
                channel_id, len(video_ids), through)
        _unavailable(lang, f"analytics query failed: {msg[:200]}",
                     channel_id, len(video_ids), through)

    rows = resp.get("rows", []) or []

    # ── aggregate daily → per-day sub/nonsub ────────────────────────────────
    # daily[isodate] = {"SUBSCRIBED": n, "UNSUBSCRIBED": n}
    daily = defaultdict(lambda: {"SUBSCRIBED": 0, "UNSUBSCRIBED": 0})
    for day_str, status, views in rows:
        if status in ("SUBSCRIBED", "UNSUBSCRIBED"):
            daily[day_str][status] += int(views)

    def window(days_back: int, key: str, label: str) -> dict:
        w_start = end_d - timedelta(days=days_back - 1)
        s = u = 0
        for day_str, d in daily.items():
            dd = date.fromisoformat(day_str)
            if w_start <= dd <= end_d:
                s += d["SUBSCRIBED"]
                u += d["UNSUBSCRIBED"]
        t = s + u
        return {
            "key": key, "label": label,
            "subscribed": s, "non_subscribed": u, "total": t,
            "pct": round(100 * s / t, 1) if t else 0.0,
        }

    windows = [
        window(7,  "7d",  "Last 7 days"),
        window(28, "28d", "Last 28 days"),
        window(90, "90d", "Last 90 days"),
    ]

    # ── weekly buckets (Monday-start), gap-filled from first data week ───────
    wk = defaultdict(lambda: {"SUBSCRIBED": 0, "UNSUBSCRIBED": 0})
    for day_str, d in daily.items():
        dd = date.fromisoformat(day_str)
        monday = dd - timedelta(days=dd.weekday())
        wk[monday]["SUBSCRIBED"] += d["SUBSCRIBED"]
        wk[monday]["UNSUBSCRIBED"] += d["UNSUBSCRIBED"]

    weeks = []
    if wk:
        first_monday = min(wk.keys())
        last_monday = end_d - timedelta(days=end_d.weekday())
        cur = first_monday
        while cur <= last_monday:
            b = wk.get(cur, {"SUBSCRIBED": 0, "UNSUBSCRIBED": 0})
            s, u = b["SUBSCRIBED"], b["UNSUBSCRIBED"]
            t = s + u
            week_sunday = cur + timedelta(days=6)
            weeks.append({
                "week_start": cur.isoformat(),
                "label": _week_label(cur),
                "subscribed": s, "non_subscribed": u, "total": t,
                "pct": round(100 * s / t, 1) if t else 0.0,
                "partial": end_d < week_sunday,   # week not yet complete
            })
            cur += timedelta(days=7)

    _emit({
        "lang": lang,
        "channel_id": channel_id,
        "through": through,
        "available": True,
        "note": None,
        "video_count": len(video_ids),
        "windows": windows,
        "weeks": weeks,
    })


if __name__ == "__main__":
    main()
