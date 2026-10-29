"""Finds a daily AI article, drafts a LinkedIn post, and makes a share image."""

import os
import glob
import hashlib
import textwrap
from datetime import datetime

from ddgs import DDGS
from PIL import Image, ImageDraw, ImageFont

import store
from contact_agent import ask_llm, parse_json, fetch_page

POST_FIELDS = ["post_id", "date", "title", "url", "source", "published",
               "summary", "post", "hook", "image_file", "created_at"]

IMAGE_DIR = os.path.join("static", "posts")

# Where the daily article comes from. Weighted toward research and analysis
# rather than product announcements, which make thinner posts.
QUERIES = [
    "AI research breakthrough paper",
    "artificial intelligence industry analysis",
    "AI model capabilities study",
    "enterprise AI adoption report",
]

# Domains whose articles tend to have substance worth commenting on.
PREFERRED = (
    "arxiv.org", "nature.com", "science.org", "technologyreview.com",
    "arstechnica.com", "theverge.com", "wired.com", "ft.com",
    "economist.com", "wsj.com", "bloomberg.com", "reuters.com",
    "venturebeat.com", "techcrunch.com", "theinformation.com",
    "stratechery.com", "anthropic.com", "openai.com", "deepmind.google",
    "semianalysis.com", "hbr.org", "mckinsey.com",
)


def _post_id(url, day):
    return hashlib.sha1(f"{day}|{url}".encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- storage

def history():
    """Every post ever generated, newest first."""
    rows = store.read_sheet(store.LINKEDIN_SHEET, POST_FIELDS)
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def latest():
    rows = history()
    return rows[0] if rows else None


def save_post(post):
    rows = history()
    rows = [r for r in rows if r.get("post_id") != post["post_id"]]
    rows.insert(0, post)
    store.write_sheet(store.LINKEDIN_SHEET, POST_FIELDS, rows[:60])


def used_urls(limit=30):
    """Recent article URLs, so we don't post about the same piece twice."""
    return {r.get("url", "") for r in history()[:limit]}


# ---------------------------------------------------------------- the article

def _search(query, log, n=6):
    try:
        return list(DDGS().news(query, region="us-en", max_results=n))
    except TypeError:
        try:
            return list(DDGS().news(query, max_results=n))
        except Exception as e:
            log(f"   search failed: {e}")
            return []
    except Exception as e:
        log(f"   search failed: {e}")
        return []


def pick_article(log=print):
    """Find one substantial AI article we have not written about yet."""
    seen = used_urls()
    candidates = []

    for q in QUERIES:
        log(f"   searching: {q}")
        for hit in _search(q, log):
            url = (hit.get("url") or hit.get("href") or "").strip()
            title = (hit.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            domain = url.split("/")[2].lower() if "://" in url else ""
            candidates.append({
                "url": url,
                "title": title,
                "snippet": (hit.get("body") or "").strip(),
                "source": (hit.get("source") or domain).strip(),
                "published": (hit.get("date") or "").strip(),
                "preferred": any(p in domain for p in PREFERRED),
            })

    if not candidates:
        log("   no fresh articles found")
        return None

    # preferred publishers first, then whatever has the meatiest snippet
    candidates.sort(key=lambda c: (not c["preferred"], -len(c["snippet"])))
    log(f"   {len(candidates)} candidates, picked: {candidates[0]['title'][:70]}")
    return candidates[0]


# ---------------------------------------------------------------- the post

def write_post(article, log=print):
    """Draft the post text. Returns (post_text, hook, summary)."""
    body = article.get("snippet", "")
    page = fetch_page(article["url"], max_chars=5000)
    if len(page) > len(body):
        body = page

    prompt = f"""You are drafting a LinkedIn post for a founder who builds AI
hiring technology. He writes plainly, as if explaining something to a friend
who does not work in tech.

ARTICLE
Title: {article['title']}
Source: {article.get('source', '')}
Text: {body[:4500]}

Write a LinkedIn post of UNDER 50 WORDS in HIS voice, first person.

Structure:
- One or two short sentences saying what happened, in plain English.
- Then HIS OPINION on it: what he thinks, whether he agrees, what it means
  for people doing the hiring. Use "I think", "I reckon", "My take" or similar.

Rules:
- UNDER 50 WORDS TOTAL. This is a hard limit. Short is the point.
- Write for someone with no technical background. No jargon. If the article
  uses a technical term, explain it in ordinary words or leave it out.
- Short sentences. Everyday words. A 12-year-old should follow it.
- He must state a view - the post is his opinion, not a summary.
- No bullet points, no emoji, no hashtags.
- No "Thoughts?", no "Excited to share", no "game-changer", no "revolutionise".
- Never claim personal involvement in the events described.
- Do not invent numbers or quotes.

Reply with ONLY this JSON:
{{"post": "", "hook": "", "summary": ""}}

- post: the full post text, under 50 words
- hook: the single most interesting idea, under 10 plain words, for the image
- summary: one sentence on what the article reports"""

    data = parse_json(ask_llm(prompt), fallback={})
    if not isinstance(data, dict):
        data = {}

    post = (data.get("post") or "").strip()
    if not post:
        log("   model gave no post text")
        post = ""

    hook = (data.get("hook") or "").strip()
    if not hook:
        hook = article["title"]

    summary = (data.get("summary") or "").strip()
    if not summary:
        summary = article.get("snippet", "")[:200]

    words = len(post.split())
    log(f"   drafted {words} words")
    return post, hook, summary


# ---------------------------------------------------------------- the image

# Each day rotates through these. (top colour, bottom colour, accent)
PALETTES = [
    ((14, 30, 62), (0, 122, 122), (120, 240, 220)),      # navy -> teal
    ((44, 12, 66), (198, 40, 120), (255, 200, 120)),     # violet -> magenta
    ((10, 42, 40), (20, 140, 90), (190, 255, 140)),      # forest -> mint
    ((60, 20, 10), (220, 90, 40), (255, 205, 120)),      # rust -> amber
    ((8, 24, 58), (60, 80, 200), (150, 200, 255)),       # midnight -> indigo
    ((48, 10, 34), (190, 40, 70), (255, 170, 160)),      # wine -> coral
    ((16, 36, 44), (26, 130, 160), (140, 230, 250)),     # slate -> cyan
]

FONT_CANDIDATES = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ],
}


def _font(weight, size):
    for path in FONT_CANDIDATES[weight]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # last resort: scan for anything usable
    for pattern in ("/System/Library/Fonts/*.ttf", "/usr/share/fonts/**/*.ttf"):
        for path in glob.glob(pattern, recursive=True)[:5]:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h), Image.LANCZOS)


def _wrap_to_fit(draw, text, font, max_width):
    words = text.split()
    lines, line = [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def make_image(hook, source, day, log=print):
    """Build a 1200x628 share card. Returns the filename, or ''."""
    os.makedirs(IMAGE_DIR, exist_ok=True)

    W, H = 1200, 628
    seed = int(hashlib.sha1(day.encode()).hexdigest()[:6], 16)
    top, bottom, accent = PALETTES[seed % len(PALETTES)]

    img = _gradient((W, H), top, bottom).convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # --- geometric decoration, deterministic per day
    d.ellipse([W - 360, -190, W + 170, 340], fill=accent + (40,))
    d.ellipse([W - 250, -80, W + 60, 230], fill=accent + (52,))
    d.ellipse([W - 150, 20, W - 10, 160], fill=accent + (70,))
    d.polygon([(0, H), (380, H), (0, H - 380)], fill=accent + (34,))
    d.polygon([(0, H), (190, H), (0, H - 190)], fill=accent + (28,))

    rng = seed
    for _ in range(11):
        rng = (rng * 1103515245 + 12345) % (2 ** 31)
        x = 60 + (rng % (W - 200))
        rng = (rng * 1103515245 + 12345) % (2 ** 31)
        y = 60 + (rng % (H - 160))
        r = 3 + (rng % 9)
        d.ellipse([x - r, y - r, x + r, y + r], fill=accent + (110,))

    # thin accent rule down the left
    d.rectangle([64, 96, 71, H - 150], fill=accent + (235,))

    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    # --- the hook, sized to fill the frame
    left, right = 104, W - 90
    max_w = right - left

    # start big for short hooks, step down until it fits in 4 lines
    for size in (104, 92, 82, 74, 66, 58, 50, 44, 38):
        f = _font("bold", size)
        lines = _wrap_to_fit(d, hook, f, max_w)
        if len(lines) <= 4 and len(lines) * size * 1.22 <= H - 250:
            break

    line_h = int(size * 1.22)
    block_h = line_h * len(lines)
    y = (H - block_h) // 2 - 26

    for line in lines:
        d.text((left + 2, y + 3), line, font=f, fill=(0, 0, 0, 70))   # soft shadow
        d.text((left, y), line, font=f, fill=(255, 255, 255, 255))
        y += line_h

    # --- footer
    fs = _font("regular", 22)
    tag = (source or "AI research").upper()[:46]
    d.text((left, H - 92), tag, font=fs, fill=accent + (255,))
    d.text((left, H - 60), day, font=fs, fill=(255, 255, 255, 150))

    fb = _font("bold", 24)
    mark = "HIRE2O"
    d.text((right - d.textlength(mark, font=fb), H - 76), mark,
           font=fb, fill=(255, 255, 255, 190))

    name = f"post-{day}.png"
    path = os.path.join(IMAGE_DIR, name)
    img.convert("RGB").save(path, "PNG", optimize=True)
    log(f"   image saved: {path}")
    return name


# ---------------------------------------------------------------- the run

def run_daily(log=print, force=False):
    """Pick an article, draft the post, render the image, store it."""
    day = datetime.now().strftime("%Y-%m-%d")

    if not force:
        current = latest()
        if current and current.get("date") == day:
            log(f"Already have today's post ({day}).")
            return current

    log("Finding today's article...")
    article = pick_article(log=log)
    if not article:
        log("ERROR: no article found")
        return None

    log(f"Article: {article['title'][:80]}")
    log("Drafting the post...")
    post, hook, summary = write_post(article, log=log)

    log("Making the image...")
    try:
        image_file = make_image(hook, article.get("source", ""), day, log=log)
    except Exception as e:
        log(f"   image failed: {e}")
        image_file = ""

    record = {
        "post_id": _post_id(article["url"], day),
        "date": day,
        "title": article["title"],
        "url": article["url"],
        "source": article.get("source", ""),
        "published": article.get("published", ""),
        "summary": summary,
        "post": post,
        "hook": hook,
        "image_file": image_file,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    save_post(record)
    log(f"Done. {len(post.split())} words, image {image_file or 'none'}.")
    return record


# ==================================================================
# Posts to comment on - paste a LinkedIn post, get a drafted comment.
# Same idea as the X tab, but LinkedIn comments run a little longer
# and read more like a considered reply than a quick quip.
# ==================================================================

import re
from datetime import timedelta, timezone

COMMENT_FIELDS = ["post_id", "author", "url", "text", "added_at",
                  "comment", "comment_at", "commented"]

COMMENT_MAX_AGE_DAYS = int(os.getenv("LI_MAX_AGE_DAYS", "7"))
COMMENT_TOP_N = int(os.getenv("LI_TOP_N", "15"))

# The author slug sits before the first underscore:
#   /posts/<author>_<post-slug>-activity-<id>-<hash>
LI_POSTS_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/posts/([A-Za-z0-9\-%]+)_[^\s]*?activity-(\d+)",
    re.I)
LI_FEED_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/feed/update/urn:li:(?:activity|ugcPost):(\d+)",
    re.I)
LI_ANY_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/\S+", re.I)


def parse_li_url(url):
    """Pull (author, activity id) out of a LinkedIn post link."""
    url = (url or "").strip()
    m = LI_POSTS_RE.search(url)
    if m:
        return m.group(1), m.group(2)
    m = LI_FEED_RE.search(url)
    if m:
        return "", m.group(1)
    return "", ""


def pretty_author(slug):
    """anurag-rohatgi-123abc -> Anurag Rohatgi"""
    if not slug:
        return ""
    parts = [p for p in slug.split("-") if p and not re.fullmatch(r"[0-9a-f]{4,}", p)]
    return " ".join(p.capitalize() for p in parts[:3])


# ---------------------------------------------------------------- storage

def comments_all():
    return store.read_sheet(store.LI_COMMENTS_SHEET, COMMENT_FIELDS)


def comments_save(rows):
    store.write_sheet(store.LI_COMMENTS_SHEET, COMMENT_FIELDS, rows)


def _li_when(value):
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def comments_recent(limit=None):
    """Inside the age window, still-to-do first, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=COMMENT_MAX_AGE_DAYS)
    live = []
    for r in comments_all():
        dt = _li_when(r.get("added_at"))
        if dt and dt < cutoff:
            continue
        item = dict(r)
        item["when"] = dt
        item["done"] = str(r.get("commented", "")).lower() in ("yes", "1", "true")
        item["who"] = pretty_author(r.get("author", "")) or r.get("author", "")
        live.append(item)

    live.sort(key=lambda x: x["when"] or datetime.now(timezone.utc), reverse=True)
    live.sort(key=lambda x: x["done"])
    return live[:(limit or COMMENT_TOP_N)]


def comment_add(url, text, log=print):
    url = (url or "").strip()
    text = (text or "").strip()
    author, activity = parse_li_url(url)

    if not activity:
        activity = hashlib.sha1((url + text).encode()).hexdigest()[:16]
    if not url and not text:
        return ""

    rows = comments_all()
    if any(r.get("post_id") == activity for r in rows):
        log("   already saved")
        return activity

    rows.insert(0, {
        "post_id": activity,
        "author": author,
        "url": url,
        "text": text,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "comment": "", "comment_at": "", "commented": "",
    })
    comments_save(rows)
    return activity


def comment_add_bulk(blob, log=print):
    """A new post starts at each linkedin.com link; text follows below it."""
    blob = (blob or "").strip()
    if not blob:
        return 0

    blocks, current = [], None
    for line in blob.splitlines():
        if LI_ANY_RE.search(line):
            if current:
                blocks.append(current)
            current = {"url": LI_ANY_RE.search(line).group(0), "lines": []}
            trailing = LI_ANY_RE.sub("", line).strip()
            if trailing:
                current["lines"].append(trailing)
        elif current is not None:
            current["lines"].append(line)
        elif line.strip():
            current = {"url": "", "lines": [line]}
    if current:
        blocks.append(current)

    added = 0
    for b in blocks:
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(b["lines"]).strip())
        if not b["url"] and not text:
            continue
        if comment_add(b["url"], text, log=log):
            added += 1

    log(f"   added {added} post(s) from {len(blocks)} block(s)")
    return added


def comment_toggle(post_id):
    rows = comments_all()
    for r in rows:
        if r.get("post_id") == post_id:
            done = str(r.get("commented", "")).lower() in ("yes", "1", "true")
            r["commented"] = "" if done else "yes"
            break
    comments_save(rows)


def comment_delete(post_id):
    comments_save([r for r in comments_all() if r.get("post_id") != post_id])


# ---------------------------------------------------------------- drafting

def draft_li_comment(post, log=print):
    """One LinkedIn comment - plain language, real understanding of the topic."""
    text = (post.get("text") or "").strip()
    if not text:
        return ""

    who = pretty_author(post.get("author", ""))
    whose = f"{who}'s" if who else "someone's"

    prompt = f"""Draft a comment on {whose} LinkedIn post.

THE POST:
{text[:1500]}

Whatever the subject is, you understand it properly - how it actually works,
where it tends to break, and what most people miss about it. You explain things
in plain words, the way you would to a smart friend who works in another field.

The comment must:
- Show real understanding of the subject. Name the mechanism, the trade-off, the
  knock-on effect, or the thing the post leaves out. That depth is the point.
- Use simple, everyday language. Short sentences. No jargon, no buzzwords. If a
  technical term is unavoidable, explain it in a few plain words.
- Add something of your own: an angle, a counterpoint, something you have seen
  happen, or a question worth answering.
- Be 30 to 60 words. Long enough to say something, short enough to read.
- Sound like a person thinking out loud, not a brand account.
- Stay on what the post actually says. Do not invent facts or numbers.

Never do these:
- No "Great post", "Well said", "Thanks for sharing", "Couldn't agree more",
  "This is so important", "Spot on".
- No emoji, no hashtags.
- Do not mention hiring, recruiting or your own work unless the post is about it.
- Do not promote anything. You are joining a conversation.
- Do not open by naming the author.

Reply with ONLY this JSON:
{{"comment": ""}}"""

    raw = ask_llm(prompt)
    if not raw:
        log("   model returned nothing")
        return ""

    data = parse_json(raw, fallback=None)
    comment = ""
    if isinstance(data, dict):
        comment = (data.get("comment") or "").strip()

    # Models often write the comment straight out instead of wrapping it in JSON.
    if not comment:
        out = raw.strip()
        out = re.sub(r"^```(?:json)?|```$", "", out).strip()
        out = re.sub(r'^\s*\{.*?"comment"\s*:\s*"?', "", out, flags=re.S)
        out = out.rstrip('"}').strip().strip('"').strip()
        if 3 < len(out) < 900:
            comment = out
            log(f"   used plain-text comment ({len(comment.split())} words)")

    if not comment:
        log(f"   could not read a comment from: {raw[:120]}")
    return comment


def draft_li_all(log=print, force=False):
    rows = comments_all()
    targets = {r["post_id"] for r in comments_recent()}
    done = 0
    log(f"   {len(rows)} saved, {len(targets)} inside the {COMMENT_MAX_AGE_DAYS}-day window")

    for r in rows:
        if r.get("post_id") not in targets:
            continue
        if r.get("comment") and not force:
            continue
        if not (r.get("text") or "").strip():
            log(f"   {r.get('author') or 'post'}: no text saved, nothing to comment on")
            continue
        log(f"   drafting comment on {pretty_author(r.get('author','')) or 'a post'}")
        c = draft_li_comment(r, log=log)
        if c:
            r["comment"] = c
            r["comment_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            done += 1

    if done:
        comments_save(rows)
    log(f"Drafted {done} comment(s).")
    return done


def redraft_li_one(post_id, log=print):
    rows = comments_all()
    for r in rows:
        if r.get("post_id") == post_id:
            c = draft_li_comment(r, log=log)
            if c:
                r["comment"] = c
                r["comment_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                comments_save(rows)
            return c
    return ""
