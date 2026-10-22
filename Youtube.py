"""Reads the AI For Recruiters YouTube channel and caches its stats."""

import os
import re
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import store

load_dotenv()

API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UC-KXsIHhVc6BhynOK0N_F1Q").strip()
CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL", "https://www.youtube.com/@AIForRecruiters").strip()

API = "https://www.googleapis.com/youtube/v3"

VIDEO_FIELDS = ["video_id", "title", "published", "published_raw", "duration",
                "views", "likes", "comments", "favorites", "privacy",
                "thumbnail", "url", "description", "tags", "fetched_at"]

CHANNEL_FIELDS = ["channel_id", "title", "handle", "subscribers", "total_views",
                  "video_count", "thumbnail", "description", "fetched_at"]


def configured():
    return bool(API_KEY and CHANNEL_ID)


def status_line():
    if not API_KEY:
        return "no API key"
    if not CHANNEL_ID:
        return "no channel ID"
    return "ready"


# ---------------------------------------------------------------- helpers

def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_duration(iso):
    """PT1H2M30S -> 1:02:30, PT4M5S -> 4:05, PT45S -> 0:45."""
    m = re.match(r"^P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return ""
    h, mi, s = (_int(m.group(1)), _int(m.group(2)), _int(m.group(3)))
    if h:
        return f"{h}:{mi:02d}:{s:02d}"
    return f"{mi}:{s:02d}"


def pretty_date(iso):
    """2026-10-14T13:05:02Z -> 2026-10-14."""
    return (iso or "")[:10]


def days_since(iso):
    day = pretty_date(iso)
    if not day:
        return None
    try:
        then = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).days


def thousands(n):
    """1234 -> 1,234"""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n or 0)


# ---------------------------------------------------------------- storage

def videos():
    """Cached videos, newest first."""
    rows = store.read_sheet(store.YOUTUBE_VIDEOS_SHEET, VIDEO_FIELDS)
    rows.sort(key=lambda r: r.get("published_raw", ""), reverse=True)
    return rows


def channel():
    rows = store.read_sheet(store.YOUTUBE_CHANNEL_SHEET, CHANNEL_FIELDS)
    return rows[0] if rows else {}


def decorate(rows):
    """Add the computed fields the page shows."""
    out = []
    for r in rows:
        views = _int(r.get("views"))
        likes = _int(r.get("likes"))
        comments = _int(r.get("comments"))
        age = days_since(r.get("published_raw"))
        item = dict(r)
        item["views_n"] = views
        item["likes_n"] = likes
        item["comments_n"] = comments
        item["views_fmt"] = thousands(views)
        item["likes_fmt"] = thousands(likes)
        item["comments_fmt"] = thousands(comments)
        # engagement = (likes + comments) / views, the usual creator metric
        item["engagement"] = round((likes + comments) / views * 100, 1) if views else 0.0
        item["age_days"] = age
        item["views_per_day"] = round(views / age, 1) if age and age > 0 else views
        out.append(item)
    return out


def totals(rows):
    """Summary numbers across every cached video."""
    v = sum(_int(r.get("views")) for r in rows)
    l = sum(_int(r.get("likes")) for r in rows)
    c = sum(_int(r.get("comments")) for r in rows)
    return {
        "videos": len(rows),
        "views": v, "views_fmt": thousands(v),
        "likes": l, "likes_fmt": thousands(l),
        "comments": c, "comments_fmt": thousands(c),
        "avg_views": thousands(round(v / len(rows))) if rows else "0",
        "engagement": round((l + c) / v * 100, 1) if v else 0.0,
    }


# ---------------------------------------------------------------- the API

def _get(path, params, log):
    params = dict(params)
    params["key"] = API_KEY
    try:
        r = requests.get(f"{API}/{path}", params=params, timeout=30)
    except Exception as e:
        log(f"   request failed: {e}")
        return None

    if r.status_code == 403:
        body = r.text[:300]
        if "quotaExceeded" in body:
            log("   quota exceeded for today - try again tomorrow")
        elif "keyInvalid" in body or "API key not valid" in body:
            log("   API key rejected - check YOUTUBE_API_KEY in .env")
        else:
            log(f"   403 from YouTube: {body}")
        return None
    if r.status_code != 200:
        log(f"   HTTP {r.status_code}: {r.text[:200]}")
        return None

    try:
        return r.json()
    except Exception as e:
        log(f"   bad JSON from YouTube: {e}")
        return None


def fetch_channel(log=print):
    """Channel stats plus the uploads playlist id."""
    data = _get("channels", {
        "part": "snippet,statistics,contentDetails",
        "id": CHANNEL_ID,
    }, log)
    if not data:
        return None, ""

    items = data.get("items") or []
    if not items:
        log(f"   no channel found for id {CHANNEL_ID}")
        return None, ""

    it = items[0]
    snip = it.get("snippet") or {}
    stats = it.get("statistics") or {}
    uploads = ((it.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", "")
    thumbs = (snip.get("thumbnails") or {})

    record = {
        "channel_id": CHANNEL_ID,
        "title": snip.get("title", ""),
        "handle": snip.get("customUrl", ""),
        "subscribers": stats.get("subscriberCount", "0"),
        "total_views": stats.get("viewCount", "0"),
        "video_count": stats.get("videoCount", "0"),
        "thumbnail": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
        "description": (snip.get("description", "") or "")[:500],
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    log(f"   channel: {record['title']} - {thousands(record['subscribers'])} subscribers, "
        f"{record['video_count']} videos")
    return record, uploads


def fetch_video_ids(uploads_playlist, log=print, cap=200):
    """Every video id on the channel, newest first."""
    ids, token = [], ""
    while len(ids) < cap:
        params = {"part": "contentDetails", "playlistId": uploads_playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _get("playlistItems", params, log)
        if not data:
            break
        for it in data.get("items", []):
            vid = ((it.get("contentDetails") or {}).get("videoId") or "").strip()
            if vid:
                ids.append(vid)
        token = data.get("nextPageToken", "")
        if not token:
            break
    return ids[:cap]


def fetch_videos(video_ids, log=print):
    """Full stats for a list of video ids, 50 at a time."""
    out = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        data = _get("videos", {
            "part": "snippet,statistics,contentDetails,status",
            "id": ",".join(batch),
        }, log)
        if not data:
            break

        for it in data.get("items", []):
            snip = it.get("snippet") or {}
            stats = it.get("statistics") or {}
            details = it.get("contentDetails") or {}
            status = it.get("status") or {}
            thumbs = snip.get("thumbnails") or {}
            vid = it.get("id", "")

            out.append({
                "video_id": vid,
                "title": snip.get("title", ""),
                "published": pretty_date(snip.get("publishedAt", "")),
                "published_raw": snip.get("publishedAt", ""),
                "duration": parse_duration(details.get("duration", "")),
                "views": stats.get("viewCount", "0"),
                "likes": stats.get("likeCount", "0"),
                "comments": stats.get("commentCount", "0"),
                "favorites": stats.get("favoriteCount", "0"),
                "privacy": status.get("privacyStatus", ""),
                "thumbnail": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "description": (snip.get("description", "") or "")[:400],
                "tags": ", ".join((snip.get("tags") or [])[:8]),
                "fetched_at": stamp,
            })

    return out


# ---------------------------------------------------------------- the run

def refresh(log=print):
    """Pull the channel and every video, and cache both."""
    if not API_KEY:
        log("ERROR: no YOUTUBE_API_KEY in .env - see the setup steps")
        return []
    if not CHANNEL_ID:
        log("ERROR: no YOUTUBE_CHANNEL_ID in .env")
        return []

    log("Reading the channel...")
    chan, uploads = fetch_channel(log=log)
    if not chan:
        return []
    store.write_sheet(store.YOUTUBE_CHANNEL_SHEET, CHANNEL_FIELDS, [chan])

    if not uploads:
        log("   no uploads playlist on this channel")
        return []

    log("Listing videos...")
    ids = fetch_video_ids(uploads, log=log)
    log(f"   {len(ids)} videos found")
    if not ids:
        store.write_sheet(store.YOUTUBE_VIDEOS_SHEET, VIDEO_FIELDS, [])
        return []

    log("Fetching stats...")
    rows = fetch_videos(ids, log=log)
    rows.sort(key=lambda r: r.get("published_raw", ""), reverse=True)
    store.write_sheet(store.YOUTUBE_VIDEOS_SHEET, VIDEO_FIELDS, rows)

    t = totals(rows)
    log(f"Done. {t['videos']} videos, {t['views_fmt']} views, "
        f"{t['likes_fmt']} likes, {t['comments_fmt']} comments.")
    return rows
