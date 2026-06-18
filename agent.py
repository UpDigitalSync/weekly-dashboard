"""
UpDigitalSync — Weekly Strategy Dashboard Agent.

Fetches data from 3 sources (TikTok via Apify, triptiplist.com via GA4,
X trends via TwitterAPI.io), runs it through Claude for structured analysis,
and renders an HTML dashboard.

Run:  python agent.py
Env:  see .env.example
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.oauth2 import service_account
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ---------- config ----------

load_dotenv()

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
ARCHIVE_DIR = ROOT / "archive"
DATA_DIR.mkdir(exist_ok=True)
ARCHIVE_DIR.mkdir(exist_ok=True)

TIKTOK_CHANNELS = ["routebites", "neuro_dispenza", "undermapped"]

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SA_JSON", "")

DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

X_QUERIES = [
    '"AI agent" OR "AI agents" min_faves:50',
    '"Claude Code" OR "Claude Cowork" min_faves:30',
    'MCP server OR "MCP tool" min_faves:30',
    '"AI automation" OR "AI employee" min_faves:50',
    'skillification OR "AI skills" min_faves:20',
]


# ---------- data fetchers ----------

def fetch_tiktok() -> dict:
    """Apify clockworks/tiktok-profile-scraper for last 10 videos per channel."""
    if not APIFY_TOKEN:
        return {"error": "APIFY_TOKEN not set", "channels": {}}

    url = (
        "https://api.apify.com/v2/acts/clockworks~tiktok-profile-scraper/"
        "run-sync-get-dataset-items"
    )
    payload = {
        "profiles": [f"https://www.tiktok.com/@{h}" for h in TIKTOK_CHANNELS],
        # Wider window so the all-time best video (which can be a viral or pinned
        # post outside the most-recent handful) is actually captured, not just the
        # last 10. The recent-momentum metrics still use the freshest 10.
        "resultsPerPage": 45,
        "shouldDownloadCovers": False,
        "shouldDownloadVideos": False,
        "shouldDownloadSubtitles": False,
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(
            url, params={"token": APIFY_TOKEN}, json=payload, headers=headers, timeout=300
        )
        resp.raise_for_status()
        items = resp.json()
    except Exception as exc:
        return {"error": f"Apify call failed: {exc}", "channels": {}}

    # group videos by author handle
    by_channel: dict[str, list] = {h: [] for h in TIKTOK_CHANNELS}
    for item in items:
        author = (item.get("authorMeta") or {}).get("name") or ""
        if author in by_channel:
            by_channel[author].append({
                "id": item.get("id"),
                "url": item.get("webVideoUrl"),
                "create_time": item.get("createTimeISO") or item.get("createTime"),
                "text": (item.get("text") or "")[:280],
                "views": item.get("playCount", 0),
                "likes": item.get("diggCount", 0),
                "comments": item.get("commentCount", 0),
                "shares": item.get("shareCount", 0),
            })

    summary = {}
    for handle in TIKTOK_CHANNELS:
        videos = sorted(
            by_channel[handle], key=lambda v: v.get("create_time") or "", reverse=True
        )
        # author meta lives on each item; pick from any video
        author_meta = next(
            (it for it in items if (it.get("authorMeta") or {}).get("name") == handle),
            None,
        ) or {}
        meta = author_meta.get("authorMeta") or {}
        all_views = [v["views"] for v in videos if v["views"]]
        recent10 = videos[:10]  # videos already sorted newest-first
        recent_views = [v["views"] for v in recent10 if v["views"]]
        best = max(videos, key=lambda v: v["views"], default=None)
        summary[handle] = {
            # All-time channel stats (impressive, reliable):
            "followers": meta.get("fans"),
            "total_likes": meta.get("heart"),
            "total_videos": meta.get("video"),
            "best_video_views": best["views"] if best else 0,
            "best_video_url": best["url"] if best else None,
            "total_views_scraped": sum(all_views),
            # Recent-momentum stats (last 10):
            "avg_views": int(sum(recent_views) / len(recent_views)) if recent_views else 0,
            "videos": recent10,
            "top_video": best,
        }
    return {"channels": summary, "fetched_at": dt.datetime.utcnow().isoformat()}


def fetch_ga4() -> dict:
    """GA4 Data API — last 7d sessions, top pages, source breakdown."""
    if not GA4_PROPERTY_ID:
        return {"error": "GA4_PROPERTY_ID not set"}

    # Load SA from env (CI) or local file (dev).
    sa_info = None
    if GOOGLE_SA_JSON:
        try:
            sa_info = json.loads(GOOGLE_SA_JSON)
        except Exception as exc:
            return {"error": f"GOOGLE_SA_JSON not valid JSON: {exc}"}
    else:
        sa_path = ROOT / "service-account.json"
        if sa_path.exists():
            sa_info = json.loads(sa_path.read_text(encoding="utf-8"))
        else:
            return {"error": "no GA4 credentials — set GOOGLE_SA_JSON or place service-account.json"}

    try:
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
        )
        client = BetaAnalyticsDataClient(credentials=creds)
    except Exception as exc:
        return {"error": f"GA4 client init failed: {exc}"}

    property_path = f"properties/{GA4_PROPERTY_ID}"

    def _run(metrics, dimensions, date_range, limit=10, order_by=None):
        req = RunReportRequest(
            property=property_path,
            metrics=[Metric(name=m) for m in metrics],
            dimensions=[Dimension(name=d) for d in dimensions],
            date_ranges=[date_range],
            limit=limit,
            order_bys=order_by or [],
        )
        return client.run_report(req)

    this_week = DateRange(start_date="7daysAgo", end_date="today")
    last_week = DateRange(start_date="14daysAgo", end_date="8daysAgo")

    try:
        totals = _run(
            ["sessions", "totalUsers", "screenPageViews", "averageSessionDuration", "bounceRate"],
            [],
            this_week,
        )
        prev_totals = _run(
            ["sessions", "totalUsers", "screenPageViews"], [], last_week
        )
        top_pages = _run(
            ["screenPageViews", "sessions"],
            ["pagePath"],
            this_week,
            limit=10,
            order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        )
        sources = _run(
            ["sessions"],
            ["sessionDefaultChannelGroup"],
            this_week,
            limit=10,
            order_by=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        )
    except Exception as exc:
        return {"error": f"GA4 query failed: {exc}"}

    def _row_values(row):
        return [v.value for v in row.metric_values]

    def _pct(curr, prev):
        if not prev:
            return None
        return round((curr - prev) / prev * 100, 1)

    cur = [float(v) for v in _row_values(totals.rows[0])] if totals.rows else [0] * 5
    prev = [float(v) for v in _row_values(prev_totals.rows[0])] if prev_totals.rows else [0] * 3

    return {
        "this_week": {
            "sessions": int(cur[0]),
            "users": int(cur[1]),
            "pageviews": int(cur[2]),
            "avg_session_sec": round(cur[3], 1),
            "bounce_rate": round(cur[4] * 100, 1),
        },
        "wow_change": {
            "sessions": _pct(cur[0], prev[0]),
            "users": _pct(cur[1], prev[1]),
            "pageviews": _pct(cur[2], prev[2]),
        },
        "top_pages": [
            {
                "path": row.dimension_values[0].value,
                "pageviews": int(row.metric_values[0].value),
                "sessions": int(row.metric_values[1].value),
            }
            for row in top_pages.rows
        ],
        "sources": [
            {
                "channel": row.dimension_values[0].value,
                "sessions": int(row.metric_values[0].value),
            }
            for row in sources.rows
        ],
        "fetched_at": dt.datetime.utcnow().isoformat(),
    }


def _snowflake_to_date(tweet_id: str) -> str:
    """Decode Twitter Snowflake ID to YYYY-MM-DD."""
    try:
        ms = (int(tweet_id) >> 22) + 1288834974657
        return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch_x_trends() -> dict:
    """TwitterAPI.io advanced search across keyword categories."""
    if not TWITTER_API_KEY:
        return {"error": "TWITTER_API_KEY not set", "tweets": []}

    base = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTER_API_KEY}
    all_tweets: list[dict] = []
    seen_ids: set[str] = set()

    for query in X_QUERIES:
        try:
            resp = requests.get(
                base,
                headers=headers,
                params={"query": query, "queryType": "Top"},
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            print(f"[x] query failed ({query[:40]}...): {exc}", file=sys.stderr)
            continue

        tweets = payload.get("tweets") or payload.get("data") or []
        for t in tweets:
            tid = str(t.get("id") or t.get("id_str") or "")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            author = (t.get("author") or {}).get("userName") or t.get("user", {}).get("screen_name") or ""
            all_tweets.append({
                "id": tid,
                "url": f"https://x.com/{author}/status/{tid}" if author else f"https://x.com/i/web/status/{tid}",
                "author": author,
                "text": (t.get("text") or "")[:500],
                "likes": t.get("likeCount") or t.get("favorite_count") or 0,
                "retweets": t.get("retweetCount") or t.get("retweet_count") or 0,
                "replies": t.get("replyCount") or t.get("reply_count") or 0,
                "date": _snowflake_to_date(tid),
                "matched_query": query,
            })

    # sort newest first by tweet date (recency, as requested)
    all_tweets.sort(key=lambda t: t["date"], reverse=True)
    return {
        "tweets": all_tweets[:30],
        "fetched_at": dt.datetime.utcnow().isoformat(),
    }


# ---------- deepseek synthesis ----------

ANALYSIS_PROMPT = """\
You are the AI strategy analyst for UpDigitalSync. Analyze the weekly data below
across our 3 business pillars and produce a strategic brief in JSON.

## Context
- TikTok: 3 channels — @routebites (travel carousels), @neuro_dispenza (motivation
  clips), @undermapped (hidden places carousels). Goal: scale to 20+ channels.
  Each channel's data includes all-time stats (followers, total_likes,
  best_video_views) and recent-momentum stats (avg_views over the last 10). For
  top_video_views in your output, use the channel's best_video_views (the
  all-time best in the scraped set), not the recent average.
- SEO: triptiplist.com (travel guides across multiple countries, growing via an
  autopilot content engine). New domain — established 07.05.2026; sitemap is
  submitted to GSC and indexing is in progress.
- Knowledge Engine: scrapes X for trends in AI agents, MCP, Claude Code, AI
  automation. Goal: discover trends worth exploring.

## Data (JSON)
{data_json}

## Output JSON shape (return ONLY the JSON, no prose):
{{
  "week": "YYYY-MM-DD",
  "tiktok": {{
    "channels": {{
      "routebites": {{"status": "growing|flat|declining", "avg_views": N, "top_video_url": "...", "top_video_views": N, "insight": "..."}},
      "neuro_dispenza": {{...}},
      "undermapped": {{...}}
    }},
    "pattern": "string describing the cross-channel pattern (what worked, what didn't)",
    "top_videos": [{{"channel": "...", "url": "...", "views": N, "why": "..."}}],
    "bottom_videos": [{{"channel": "...", "url": "...", "views": N, "diagnosis": "..."}}]
  }},
  "seo": {{
    "sessions": N,
    "wow_change_pct": N,
    "top_pages": [{{"path": "...", "pageviews": N}}],
    "starting_to_rank": [{{"path": "...", "note": "..."}}],
    "gaps": ["..."],
    "insight": "is traffic growing? where are the gaps?"
  }},
  "knowledge_engine": {{
    "trends": [
      {{"title": "...", "summary": "...", "url": "https://x.com/...", "date": "YYYY-MM-DD", "relevance": "high|medium|low", "action": "..."}}
    ]
  }},
  "flags": [{{"pillar": "tiktok|seo|knowledge", "msg": "..."}}],
  "priorities": [
    {{"rank": 1, "action": "...", "pillar": "tiktok|seo|knowledge", "effort": "low|medium|high", "impact": "low|medium|high"}}
  ]
}}

Rules:
- trends MUST sort newest-first by date.
- trends MUST include real URLs as they appear in the input data.
- if a data source returned an error, set the matching section to nulls and
  add a flag explaining what is missing.
- max 7 priorities, max 7 trends.
"""


def synthesize_with_deepseek(tiktok: dict, ga4: dict, trends: dict) -> dict:
    if not DEEPSEEK_API_KEY:
        # graceful fallback — return raw data so the template still renders
        return {
            "week": dt.date.today().isoformat(),
            "tiktok": {"channels": {}, "pattern": "DEEPSEEK_API_KEY not set", "top_videos": [], "bottom_videos": []},
            "seo": {"sessions": None, "insight": "DEEPSEEK_API_KEY not set"},
            "knowledge_engine": {"trends": []},
            "flags": [{"pillar": "system", "msg": "DEEPSEEK_API_KEY not set — analysis skipped"}],
            "priorities": [],
            "raw": {"tiktok": tiktok, "ga4": ga4, "trends": trends},
        }

    data_json = json.dumps(
        {"tiktok": tiktok, "ga4": ga4, "trends": trends}, default=str
    )[:60000]  # keep within deepseek-chat context window

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        max_tokens=4096,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a strategic analyst that always replies with a single valid JSON object matching the requested schema. No prose."},
            {"role": "user", "content": ANALYSIS_PROMPT.format(data_json=data_json)},
        ],
    )
    text = resp.choices[0].message.content.strip()
    # defensive: strip ```json fence if model added one despite response_format
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


# ---------- render ----------

def fetch_article_count() -> int:
    """Live count of published articles from the triptiplist sitemap — country
    article URLs only (exclude homepage and country index pages). Keeps the
    'Site Articles Live' KPI honest instead of a hardcoded number."""
    try:
        r = requests.get("https://triptiplist.com/sitemap.xml", timeout=30)
        r.raise_for_status()
        locs = re.findall(r"<loc>(.*?)</loc>", r.text)
        count = 0
        for u in locs:
            path = u.replace("https://triptiplist.com", "").strip("/")
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2:  # {country}/{slug} = an article (not / or /country/)
                count += 1
        return count
    except Exception:
        return 0


def render_html(analysis: dict, raw: dict, articles_live: int) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")
    return template.render(
        analysis=analysis,
        raw=raw,
        articles_live=articles_live,
        generated_at=dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        week_iso=dt.date.today().isoformat(),
    )


# ---------- main ----------

def main() -> int:
    today = dt.date.today().isoformat()
    print(f"[agent] week of {today}")

    tiktok = fetch_tiktok()
    print(f"[tiktok] {'ok' if 'error' not in tiktok else tiktok['error']}")

    ga4 = fetch_ga4()
    print(f"[ga4] {'ok' if 'error' not in ga4 else ga4['error']}")

    trends = fetch_x_trends()
    print(f"[x] {'ok' if 'error' not in trends else trends['error']}")

    # persist raw data for debugging
    (DATA_DIR / f"{today}.json").write_text(
        json.dumps({"tiktok": tiktok, "ga4": ga4, "trends": trends}, indent=2, default=str),
        encoding="utf-8",
    )

    try:
        analysis = synthesize_with_deepseek(tiktok, ga4, trends)
    except Exception as exc:
        traceback.print_exc()
        analysis = {
            "week": today,
            "flags": [{"pillar": "system", "msg": f"DeepSeek call failed: {exc}"}],
            "priorities": [],
            "tiktok": {"channels": {}, "pattern": "", "top_videos": [], "bottom_videos": []},
            "seo": {},
            "knowledge_engine": {"trends": []},
        }

    articles_live = fetch_article_count()
    print(f"[articles] live count from sitemap: {articles_live}")
    html = render_html(analysis, raw={"tiktok": tiktok, "ga4": ga4, "trends": trends},
                       articles_live=articles_live)

    (ROOT / "index.html").write_text(html, encoding="utf-8")
    (ARCHIVE_DIR / f"week-{today}.html").write_text(html, encoding="utf-8")
    print(f"[render] wrote index.html and archive/week-{today}.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
