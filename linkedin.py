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
hiring technology. He writes as an operator and architect - practical,
sceptical of hype, interested in what things mean rather than what was announced.

ARTICLE
Title: {article['title']}
Source: {article.get('source', '')}
Text: {body[:4500]}

Write a LinkedIn post of 100-150 words in HIS voice, first person.

What the post must do:
- Open with one sharp line that states a position, not a summary.
- Say what the article reports, briefly, so a reader who hasn't seen it follows.
- Then give the analysis: what this implies, what most people will miss,
  or where the second-order effect lands. This is the substance.
- Close with a question or an observation that invites a reply.

Rules:
- Plain prose. No bullet points, no emoji, no hashtags.
- No "Thoughts?", no "Excited to share", no "game-changer", no "revolutionise".
- Never claim personal involvement in the events described.
- Draw only on what the article says. Do not invent numbers or quotes.
- Between 100 and 150 words.

Reply with ONLY this JSON:
{{"post": "", "hook": "", "summary": ""}}

- post: the full post text
- hook: the opening line alone, under 12 words, for the image
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
