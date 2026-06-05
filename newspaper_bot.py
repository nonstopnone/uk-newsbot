#!/usr/bin/env python3
"""
"""

import os
import re
import sys
import time
import tempfile
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import praw


# ── 1. Configuration ──────────────────────────────────────────────────────────

def _clean_env(name, default=None, required=False):
    """Read env var and strip whitespace/newlines.

    GitHub Actions secrets can pick up trailing whitespace or newlines when
    pasted from a clipboard; any leading/trailing whitespace makes HTTP
    header values invalid. Always sanitise.
    """
    val = os.environ.get(name, "")
    val = val.strip() if val else ""
    if not val:
        if required:
            raise RuntimeError(f"Required env var {name!r} is missing or empty")
        return default
    return val


REDDIT_CLIENT_ID     = _clean_env("REDDIT_CLIENT_ID",     required=True)
REDDIT_CLIENT_SECRET = _clean_env("REDDIT_CLIENT_SECRET", required=True)
REDDIT_USERNAME      = _clean_env("REDDIT_USERNAME",      required=True)
REDDIT_PASSWORD      = _clean_env("REDDITPASSWORD",       required=True)  # no underscore – deliberate
SUBREDDIT_NAME       = _clean_env("SUBREDDIT", default="uknews_approvals")

# USER_AGENT secret is optional; if empty/whitespace-only, build a default.
# Without this fallback, PRAW concatenates "" + " PRAW/x.y.z" = " PRAW/..."
# which has a leading space and is rejected as an invalid HTTP header.
USER_AGENT = _clean_env("USER_AGENT") or f"python:uk-papers-bot:v1.0 (by /u/{REDDIT_USERNAME})"

TOPIC_URL  = "https://www.bbc.co.uk/news/topics/cpml2v678pxt"
BBC_BASE   = "https://www.bbc.co.uk"
IMG_WIDTH  = 1024   # px width to request from BBC CDN (they resize dynamically)

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UKNewsPapersBot/1.0)",
    "Accept-Language": "en-GB,en;q=0.9",
}


# ── 2. Scrape topic page → today's article URL ───────────────────────────────

def get_latest_article_url():
    """Return the URL of the most-recent BBC The Papers article."""
    resp = requests.get(TOPIC_URL, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    link = soup.select_one("a[href^='/news/articles/']")
    if not link:
        raise RuntimeError("No article link found on BBC The Papers topic page")

    return BBC_BASE + link["href"]


# ── 3. Scrape article page → list of paper dicts ────────────────────────────

def _parse_article_date(soup):
    """Return (day_name, date_str) from the article's <time> element.

    Prefers the human text ("31 May 2026, 00:22 BST") which is already in
    UK local time, avoiding timezone library dependencies.
    """
    time_el = soup.find("time", {"datetime": True})
    if time_el:
        text = time_el.get_text(strip=True)
        m = re.match(r"(\d{1,2}) (\w+) (\d{4})", text)
        if m:
            dt = datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
            )
            return dt.strftime("%A"), dt.strftime("%d/%m/%Y")
    dt = datetime.utcnow()
    return dt.strftime("%A"), dt.strftime("%d/%m/%Y")


def _paper_name_from_alt(alt):
    """Extract paper name from BBC alt text.

    Pattern: "The headline on the front page of the Sunday Times reads: …"
    Returns: "Sunday Times"
    """
    m = re.search(r"front page of (?:the )?(.+?)\s+reads", alt, re.IGNORECASE)
    return m.group(1).strip() if m else None


def get_papers(article_url):
    """Scrape the article page; return list of paper dicts."""
    resp = requests.get(article_url, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    day_name, date_str = _parse_article_date(soup)

    papers = []
    seen = set()

    for fig in soup.find_all("figure"):
        img = fig.find("img")
        if not img:
            continue

        alt = (img.get("alt") or "").strip()
        if "front page of" not in alt.lower():
            continue   # skip News Daily banners, promo images, etc.

        paper_name = _paper_name_from_alt(alt)
        if not paper_name or paper_name in seen:
            continue
        seen.add(paper_name)

        src = (img.get("src") or "").strip()
        if not src:
            continue
        image_url = re.sub(r"/ace/standard/\d+/", f"/ace/standard/{IMG_WIDTH}/", src)

        caption = fig.find("figcaption")
        blurb = ""
        if caption:
            blurb = " ".join(
                p.get_text(" ", strip=True) for p in caption.find_all("p")
            ).strip()
        if not blurb:
            blurb = f"BBC The Papers – {paper_name}"

        papers.append({
            "name":      paper_name,
            "image_url": image_url,
            "blurb":     blurb,
            "day_name":  day_name,
            "date_str":  date_str,
        })

    return papers


# ── 4. Reddit helpers ─────────────────────────────────────────────────────────

def make_title(paper_name, day_name, date_str):
    """e.g. "Sunday Times | Sunday 31/05/2026"."""
    return f"{paper_name} | {day_name} {date_str}".strip()


def get_existing_titles(subreddit, limit=100):
    """Cache subreddit.new() titles for dedup. Called once per run."""
    try:
        return {sub.title for sub in subreddit.new(limit=limit)}
    except Exception as e:
        print(f"  WARN: could not fetch existing titles: {e}")
        return set()


def _find_submission(me, subreddit, title, max_wait=60):
    """Poll for our newly-created submission.

    submit_image(..., without_websockets=True) returns None, so we locate
    the post by scanning recent submissions. Waits up to max_wait seconds.
    """
    poll_interval = 3
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(poll_interval)
        # Fast path: bot's own recent submissions
        try:
            for sub in me.submissions.new(limit=5):
                if sub.title == title:
                    return sub
        except Exception as e:
            print(f"  WARN: polling user submissions failed: {e}")
        # Fallback: subreddit's new queue, filtered by author
        try:
            for sub in subreddit.new(limit=20):
                if sub.title == title and str(sub.author) == REDDIT_USERNAME:
                    return sub
        except Exception:
            pass
    return None


def post_paper(subreddit, me, paper, existing_titles):
    """Post one paper. Returns True if posted, False if skipped/failed."""
    title = make_title(paper["name"], paper["day_name"], paper["date_str"])

    if title in existing_titles:
        print(f"  SKIP  {title}")
        return False

    print(f"  POST  {title}")

    # 1. Download image
    try:
        img_resp = requests.get(paper["image_url"], headers=HTTP_HEADERS, timeout=30)
        img_resp.raise_for_status()
    except Exception as e:
        print(f"        ERROR downloading image: {e}")
        return False

    content_type = img_resp.headers.get("content-type", "image/jpeg").lower()
    ext = "png" if "png" in content_type else "jpg"

    # 2. Submit image (PRAW needs a real file path, not a BytesIO)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp.write(img_resp.content)
            tmp_path = tmp.name

        subreddit.submit_image(
            title=title,
            image_path=tmp_path,
            without_websockets=True,   # avoid websocket hang on CI runners
        )
    except Exception as e:
        print(f"        ERROR submitting image: {e}")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # 3. Locate the submission and attach the blurb as first comment
    submission = _find_submission(me, subreddit, title)
    if submission:
        try:
            submission.reply(paper["blurb"])
            print(f"        → https://reddit.com{submission.permalink}")
        except Exception as e:
            print(f"        WARN: could not post blurb comment: {e}")
    else:
        print(f"        WARN: submission not found within timeout – blurb not posted")

    existing_titles.add(title)
    return True


# ── 5. Main ───────────────────────────────────────────────────────────────────

def main():
    # ─ Step 1: Authenticate with Reddit ────────────────────────────────────
    print("Step 1: Connecting to Reddit…")
    print(f"        Using user agent: {USER_AGENT!r}")
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent=USER_AGENT,
        ratelimit_seconds=300,
    )
    reddit.validate_on_submit = True
    me = reddit.user.me()
    if me is None:
        raise RuntimeError("Reddit authentication returned no user — check credentials.")
    subreddit = reddit.subreddit(SUBREDDIT_NAME)
    print(f"        Authenticated as u/{me.name}, posting to r/{SUBREDDIT_NAME}")

    # ─ Step 2: Find the latest BBC The Papers article ───────────────────────
    print("Step 2: Finding today's BBC The Papers article…")
    article_url = get_latest_article_url()
    print(f"        {article_url}")

    # ─ Step 3: Scrape front-page images and blurbs ──────────────────────────
    print("Step 3: Scraping paper images and blurbs…")
    papers = get_papers(article_url)
    print(f"        Found {len(papers)} papers")
    if not papers:
        print("        Nothing to post – exiting.")
        sys.exit(0)

    # ─ Step 4: Load existing post titles for dedup (one call) ───────────────
    print("Step 4: Loading recent posts for dedup check…")
    existing_titles = get_existing_titles(subreddit)
    print(f"        {len(existing_titles)} existing titles cached")

    # ─ Step 5: Post each paper ──────────────────────────────────────────────
    print("Step 5: Posting papers…")
    posted = skipped = errors = 0
    for paper in papers:
        try:
            if post_paper(subreddit, me, paper, existing_titles):
                posted += 1
                time.sleep(2)   # brief buffer between posts
            else:
                skipped += 1
        except Exception as exc:
            print(f"  ERROR {paper['name']}: {exc}")
            errors += 1

    print(f"\nDone – posted: {posted}  skipped: {skipped}  errors: {errors}")


if __name__ == "__main__":
    main()
