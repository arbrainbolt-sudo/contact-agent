"""X (Twitter) tab - recent posts worth replying to, with a drafted comment.

Two ways in:

  1. Paste.  Drop a post's link and text into the page. Works today, free.
  2. X API.  If X_ACCESS_TOKEN is set, pulls your following-timeline directly.
     Needs a paid X tier and an OAuth 2.0 user token - see setup_notes().

Either way the drafting is the same: one LLM call turns each post into a
reply you could actually send.
"""

import os
import re
import html
import hashlib
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

import store
from contact_agent import ask_llm, parse_json

load_dotenv()

HANDLE = os.getenv("X_HANDLE", "hire2oai").lstrip("@").strip()
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "").strip()      # OAuth 2.0 user token
USER_ID = os.getenv("X_USER_ID", "").strip()                # numeric id, optional

API = "https://api.x.com/2"

POST_FIELDS = ["post_id", "author", "author_name", "url", "text", "posted_at",
               "likes", "reposts", "replies", "comment", "comment_at",
               "commented", "added_at", "source"]

MAX_AGE_DAYS = int(os.getenv("X_MAX_AGE_DAYS", "3"))        # nothing older shows
PURGE_AFTER_DAYS = 21                                       # housekeeping
TOP_N = int(os.getenv("X_TOP_N", "10"))

URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)")


def api_ready():
    return bool(ACCESS_TOKEN)


def status_line():
    return "X API connected" if api_ready() else "paste mode"


def setup_notes():
    return [
        "Create a project and app at developer.x.com, on a tier that includes read access.",
        "Add a redirect URL and enable OAuth 2.0 with the tweet.read and users.read scopes.",
        "Run the OAuth 2.0 PKCE flow once to get a user access token.",
        "Put it in .env as X_ACCESS_TOKEN=... then restart.",
    ]


# ---------------------------------------------------------------- helpers

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_post_url(url):
    """x.com/someone/status/123 -> ('someone', '123'). ('', '') if it isn't one."""
    m = URL_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def _now():
    return datetime.now(timezone.utc)


def _parse_when(value):
    """ISO timestamp, a plain date, or blank."""
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v.replace("Z", "+0000"), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def age_label(value):
    dt = _parse_when(value)
    if not dt:
        return ""
    hours = (_now() - dt).total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


# ---------------------------------------------------------------- storage

def all_posts():
    return store.read_sheet(store.X_POSTS_SHEET, POST_FIELDS)


def save_all(rows):
    store.write_sheet(store.X_POSTS_SHEET, POST_FIELDS, rows)


def recent(limit=None):
    """Posts inside the age window, best first, capped at TOP_N."""
    cutoff = _now() - timedelta(days=MAX_AGE_DAYS)
    live = []
    for r in all_posts():
        dt = _parse_when(r.get("posted_at"))
        if dt and dt < cutoff:
            continue
        item = dict(r)
        item["when"] = dt
        item["age"] = age_label(r.get("posted_at"))
        item["done"] = str(r.get("commented", "")).lower() in ("yes", "1", "true")
        item["engagement"] = (_int(r.get("likes")) + _int(r.get("reposts")) * 2
                              + _int(r.get("replies")) * 3)
        live.append(item)

    # still-to-do first; within that, most engagement, then newest
    live.sort(key=lambda x: (x["engagement"], x["when"] or _now()), reverse=True)
    live.sort(key=lambda x: x["done"])
    return live[:(limit or TOP_N)]


def purge_old(log=print):
    """Drop anything well past the window so the sheet stays small."""
    cutoff = _now() - timedelta(days=PURGE_AFTER_DAYS)
    rows = all_posts()
    kept = [r for r in rows
            if not (_parse_when(r.get("posted_at")) and
                    _parse_when(r.get("posted_at")) < cutoff)]
    if len(kept) != len(rows):
        save_all(kept)
        log(f"   purged {len(rows) - len(kept)} old posts")
    return len(rows) - len(kept)


def add_post(url, text, author="", posted_at="", log=print):
    """Store one pasted post. Returns its id, or '' if it was no good."""
    handle, status_id = parse_post_url(url)
    author = (author or handle).lstrip("@").strip()
    text = (text or "").strip()

    if not status_id:
        status_id = hashlib.sha1((url + text).encode()).hexdigest()[:16]
    if not text and not url:
        return ""

    rows = all_posts()
    if any(r.get("post_id") == status_id for r in rows):
        log("   already saved")
        return status_id

    rows.insert(0, {
        "post_id": status_id,
        "author": author,
        "author_name": "",
        "url": url.strip() or (f"https://x.com/{author}/status/{status_id}"
                               if author else ""),
        "text": text,
        "posted_at": posted_at or _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "likes": "", "reposts": "", "replies": "",
        "comment": "", "comment_at": "", "commented": "",
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "paste",
    })
    save_all(rows)
    return status_id


def add_bulk(blob, log=print):
    """Several posts at once. Blocks separated by a blank line, link first."""
    added = 0
    for block in re.split(r"\n\s*\n", blob or ""):
        block = block.strip()
        if not block:
            continue
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        url = next((l for l in lines if URL_RE.search(l)), "")
        text = " ".join(l for l in lines if l != url).strip()
        if not url and not text:
            continue
        if add_post(url, text, log=log):
            added += 1
    log(f"   added {added} post(s)")
    return added


def toggle_commented(post_id):
    """Tick or untick 'I have replied to this'. Returns the new state."""
    rows = all_posts()
    state = ""
    for r in rows:
        if r.get("post_id") == post_id:
            state = "" if str(r.get("commented", "")).lower() in ("yes", "1", "true") else "yes"
            r["commented"] = state
            break
    save_all(rows)
    return state


def delete_post(post_id):
    save_all([r for r in all_posts() if r.get("post_id") != post_id])


# ---------------------------------------------------------------- drafting

def draft_comment(post, log=print):
    """One reply, in his voice. Returns the text, or ''."""
    author = post.get("author") or "someone"
    text = (post.get("text") or "").strip()
    if not text:
        return ""

    prompt = f"""Draft a reply to this post on X.

POST by @{author}:
{text[:1200]}

You are replying as @{HANDLE} - a founder who builds AI hiring and candidate
vetting technology. He is an operator, practical, and reads hype sceptically.

The reply must:
- Add something. A specific angle, a counterpoint, a short example, or a
  question worth answering. Never just agree.
- Be under 40 words. Replies on X are short.
- Sound like a person talking, not a brand account.
- Stay on what the post actually says. Do not invent facts or numbers.

Never do these:
- No "Great post!", "Well said", "Couldn't agree more", "This!"
- No emoji, no hashtags.
- No pitching his company. He is joining a conversation, not advertising.
- Do not open with the author's name.

Reply with ONLY this JSON:
{{"comment": ""}}"""

    raw = ask_llm(prompt)
    if not raw:
        log(f"   @{author}: model returned nothing")
        return ""

    data = parse_json(raw, fallback=None)
    comment = ""
    if isinstance(data, dict):
        comment = (data.get("comment") or "").strip()

    # Free models often ignore "reply with JSON" and just write the reply.
    # Use it rather than throwing a perfectly good sentence away.
    if not comment:
        text_out = raw.strip()
        text_out = re.sub(r"^```(?:json)?|```$", "", text_out).strip()
        text_out = re.sub(r'^\s*\{.*?"comment"\s*:\s*"?', "", text_out, flags=re.S)
        text_out = text_out.rstrip('"}').strip()
        text_out = text_out.strip('"').strip()
        if 3 < len(text_out) < 600:
            comment = text_out
            log(f"   @{author}: used plain-text reply ({len(comment.split())} words)")

    if not comment:
        log(f"   @{author}: could not read a reply from: {raw[:120]}")
    return comment


def draft_all(log=print, force=False):
    """Draft a comment for every recent post that lacks one."""
    rows = all_posts()
    targets = {r["post_id"] for r in recent()}
    done = 0
    log(f"   {len(rows)} saved, {len(targets)} inside the {MAX_AGE_DAYS}-day window")
    if not targets:
        log("   nothing to draft - add posts below, or they are all too old")

    for r in rows:
        if r.get("post_id") not in targets:
            continue
        if r.get("comment") and not force:
            continue
        if not (r.get("text") or "").strip():
            log(f"   @{r.get('author', '?')}: no post text saved, nothing to reply to")
            continue
        log(f"   drafting reply to @{r.get('author', '?')}")
        comment = draft_comment(r, log=log)
        if comment:
            r["comment"] = comment
            r["comment_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            done += 1

    if done:
        save_all(rows)
    log(f"Drafted {done} comment(s).")
    return done


def redraft_one(post_id, log=print):
    rows = all_posts()
    for r in rows:
        if r.get("post_id") == post_id:
            comment = draft_comment(r, log=log)
            if comment:
                r["comment"] = comment
                r["comment_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                save_all(rows)
            return comment
    return ""


# ---------------------------------------------------------------- the X API
#
# Dormant until X_ACCESS_TOKEN is set. The endpoint below needs user-context
# auth and a paid tier - a plain app bearer token will not open it.

def _api(path, params, log):
    try:
        r = requests.get(f"{API}/{path}",
                         headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                         params=params, timeout=30)
    except Exception as e:
        log(f"   request failed: {e}")
        return None

    if r.status_code == 401:
        log("   401 - token rejected or expired; run the OAuth flow again")
        return None
    if r.status_code == 403:
        log(f"   403 - your X tier does not allow this endpoint: {r.text[:200]}")
        return None
    if r.status_code == 429:
        log("   429 - rate limited, try later")
        return None
    if r.status_code != 200:
        log(f"   HTTP {r.status_code}: {r.text[:200]}")
        return None
    try:
        return r.json()
    except Exception as e:
        log(f"   bad JSON: {e}")
        return None


def resolve_user_id(log=print):
    if USER_ID:
        return USER_ID
    data = _api(f"users/by/username/{HANDLE}", {}, log)
    return ((data or {}).get("data") or {}).get("id", "")


def fetch_timeline(log=print):
    """Posts from the accounts @HANDLE follows, newest first."""
    if not api_ready():
        log("   no X_ACCESS_TOKEN - staying in paste mode")
        return []

    uid = resolve_user_id(log=log)
    if not uid:
        log("   could not resolve the user id")
        return []

    start = (_now() - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _api(f"users/{uid}/timelines/reverse_chronological", {
        "max_results": 100,
        "start_time": start,
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }, log)
    if not data:
        return []

    users = {u["id"]: u for u in ((data.get("includes") or {}).get("users") or [])}
    out = []
    for t in data.get("data", []):
        u = users.get(t.get("author_id"), {})
        metrics = t.get("public_metrics") or {}
        handle = u.get("username", "")
        out.append({
            "post_id": t.get("id", ""),
            "author": handle,
            "author_name": u.get("name", ""),
            "url": f"https://x.com/{handle}/status/{t.get('id','')}",
            "text": html.unescape(t.get("text", "")),
            "posted_at": t.get("created_at", ""),
            "likes": str(metrics.get("like_count", 0)),
            "reposts": str(metrics.get("retweet_count", 0)),
            "replies": str(metrics.get("reply_count", 0)),
            "comment": "", "comment_at": "", "commented": "",
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": "api",
        })
    log(f"   {len(out)} posts from the timeline")
    return out


def refresh(log=print):
    """Pull the timeline if the API is on, then draft any missing comments."""
    purge_old(log=log)

    if api_ready():
        log("Reading your X timeline...")
        fetched = fetch_timeline(log=log)
        if fetched:
            rows = all_posts()
            known = {r.get("post_id") for r in rows}
            fresh = [f for f in fetched if f["post_id"] not in known]
            if fresh:
                save_all(fresh + rows)
                log(f"   {len(fresh)} new")
    else:
        log("Paste mode - add posts below, then draft comments.")

    return draft_all(log=log)
