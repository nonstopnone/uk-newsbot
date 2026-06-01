#!/usr/bin/env python3
"""BBC The Papers → Reddit image poster

Scrapes today's newspaper front-page images and BBC editorial blurbs from
the BBC "The Papers" feature, then posts each one as an image post to
the configured subreddit with the blurb as the first comment.

Idempotent: checks subreddit.new() titles before posting.
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

def _e(key):
    """Read env var and strip whitespace (guards against trailing-space secrets)."""
    return os.environ[key].strip()

REDDIT_CLIENT_ID     = _e("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _e("REDDIT_CLIENT_SECRET")
REDDIT_USERNAME      = _e("REDDIT_USERNAME")
REDDIT_PASSWORD      = _e("REDDITPASSWORD")   # no underscore – deliberate
USER_AGENT           = _e("USER_AGENT")
SUBREDDIT_NAME       = os.environ.get("SUBREDDIT", "uknews_approvals").strip()

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

    Prefers the text content ("31 May 2026, 00:22 BST") which is already
    in UK local time, avoiding timezone library dependencies.
    """
    time_el = soup.find("time", {"datetime": True})
    if time_el:
        text = time_el.get_text(strip=True)        # "31 May 2026, 00:22 BST"
        m = re.match(r"(\d{1,2}) (\w+) (\d{4})", text)
        if m:
            dt = datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
            )
            return dt.strftime("%A"), dt.strftime("%d/%m/%Y")

    # Fallback: UTC today
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

        alt = img.get("alt", "")
        if "front page of" not in alt.lower():
            continue   # News Daily banners, promo images, etc.

        paper_name = _paper_name_from_alt(alt)
        if not paper_name or paper_name in seen:
            continue
        seen.add(paper_name)

        # Build image URL at desired display width
        src = img.get("src", "")
        if not src:
            continue
        image_url = re.sub(r"/ace/standard/\d+/", f"/ace/standard/{IMG_WIDTH}/", src)

        # Extract blurb from figcaption paragraphs
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
    """e.g. "Sunday Times | Sunday 31/05/2026" """
    return f"{paper_name} | {day_name} {date_str}"


def get_existing_titles(subreddit, limit=100):
    """Cache subreddit.new() titles for dedup. Called ONCE."""
    return {sub.title for sub in subreddit.new(limit=limit)}


def _find_submission(me, subreddit, title, max_wait=30):
    """Poll for our newly-created submission.

    submit_image(..., without_websockets=True) returns None, so we locate
    the post by scanning the bot account's recent submissions.
    Waits up to max_wait seconds.
    """
    for _ in range(max_wait // 5):
        time.sleep(5)
        # Check user's recent submissions first (fastest path)
        for sub in me.submissions.new(limit=3):
            if sub.title == title:
                return sub
        # Fallback: scan subreddit new queue
        for sub in subreddit.new(limit=15):
            if sub.title == title:
                return sub
    return None


def post_paper(subreddit, me, paper, existing_titles):
    """Post one paper. Returns True if posted, False if skipped."""
    title = make_title(paper["name"], paper["day_name"], paper["date_str"])

    if title in existing_titles:
        print(f"  SKIP  {title}")
        return False

    print(f"  POST  {title}")

    # Download image
    img_resp = requests.get(paper["image_url"], headers=HTTP_HEADERS, timeout=30)
    img_resp.raise_for_status()

    content_type = img_resp.headers.get("content-type", "image/jpeg")
    ext = "png" if "png" in content_type else "jpg"

    # PRAW's submit_image needs a file path, not a BytesIO
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(img_resp.content)
        tmp_path = tmp.name

    try:
        subreddit.submit_image(
            title=title,
            image_path=tmp_path,
            without_websockets=True,   # avoid websocket hang on CI runners
        )
    finally:
        os.unlink(tmp_path)

    # without_websockets=True returns None; locate the submission to comment
    submission = _find_submission(me, subreddit, title)
    if submission:
        submission.reply(paper["blurb"])
        print(f"        → https://reddit.com{submission.permalink}")
    else:
        print(f"        WARNING: submission not found within timeout – blurb not posted")

    existing_titles.add(title)
    return True


# ── 5. Main ───────────────────────────────────────────────────────────────────

def main():
    # ─ Step 1: Authenticate with Reddit ────────────────────────────────────
    print("Step 1: Connecting to Reddit…")
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent=USER_AGENT,
    )
    me        = reddit.user.me()
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
