#!/usr/bin/env python3
"""
Daily Newspaper Bot.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import praw
import requests
from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from prawcore.exceptions import PrawcoreException


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UK_TZ = ZoneInfo("Europe/London")

DEFAULT_SUBREDDIT = "uknews_approvals"

# Sky News uses a slug like "tuesdays-national-newspaper-front-pages-<ID>".
# The day prefix rotates with the day of the week the article is about; the ID
# at the end is the article's database ID and (in our testing) is stable.
SKY_ARTICLE_ID_DEFAULT = "12427754"
SKY_URL_TEMPLATE = "https://news.sky.com/story/{day_lower}s-national-newspaper-front-pages-{article_id}"
SKY_URL_FALLBACK = "https://news.sky.com/story/{article_id}"  # bare ID; Sky often redirects to canonical

SCAN_LIMIT = 15                # how many posts from the top of the live blog to look at
STALE_HOURS = 18               # skip posts older than this
INTER_POST_SLEEP = 6           # seconds between Reddit submissions
IMAGE_MIN_BYTES = 4_096        # anything smaller is almost certainly a placeholder / 404
HTTP_TIMEOUT = 30
PAGE_LOAD_TIMEOUT = 45_000     # ms
SELECTOR_TIMEOUT = 30_000      # ms

# Realistic browser UA — Sky News may otherwise block headless Chromium.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("papers")


# ---------------------------------------------------------------------------
# Time / URL helpers
# ---------------------------------------------------------------------------

def expected_paper_day() -> str:
    """
    Day-of-week name (e.g. "Tuesday") that the current Sky News article is FOR.

    Sky News posts "Tomorrow's papers" from ~22:00 UK time. The article stays
    live through the next morning. So:
      * Evening (UK hour >= 18)  -> tomorrow's day name
      * Morning (UK hour <  18)  -> today's day name (article from last night)
    """
    now_uk = datetime.now(UK_TZ)
    target = now_uk + timedelta(days=1) if now_uk.hour >= 18 else now_uk
    return target.strftime("%A")


def expected_paper_date() -> datetime:
    now_uk = datetime.now(UK_TZ)
    return now_uk + timedelta(days=1) if now_uk.hour >= 18 else now_uk


def candidate_urls(article_id: str) -> list[str]:
    """URLs to try in order. Today's expected slug first, then the
    previous day's slug (for edge cases where the article hasn't yet rotated),
    then the bare-ID URL which Sky often redirects to the canonical slug."""
    now_uk = datetime.now(UK_TZ)
    today_paper = expected_paper_day()
    # The "other" day to try is the one we'd be on if we were on the opposite
    # side of the 18:00 cutoff.
    other = (now_uk - timedelta(days=1) if now_uk.hour >= 18 else now_uk + timedelta(days=1)).strftime("%A")
    seen, urls = set(), []
    for day in (today_paper, other):
        u = SKY_URL_TEMPLATE.format(day_lower=day.lower(), article_id=article_id)
        if u not in seen:
            urls.append(u)
            seen.add(u)
    urls.append(SKY_URL_FALLBACK.format(article_id=article_id))
    return urls


def parse_post_time(post_text: str) -> Optional[datetime]:
    """Best-effort parse of a 'HH:MM' timestamp from a live-blog post header.

    We only look at the FIRST line to avoid matching times mentioned in the body.
    """
    first_line = (post_text.splitlines() or [""])[0]
    m = re.match(r"\s*([0-1]?[0-9]|2[0-3]):([0-5][0-9])\s*$", first_line)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    now = datetime.now(UK_TZ)
    dt_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return dt_today if dt_today <= now else dt_today - timedelta(days=1)


# ---------------------------------------------------------------------------
# Reddit helpers
# ---------------------------------------------------------------------------

def _clean_env(name: str) -> str:
    """Read an env var and strip surrounding whitespace/newlines.

    GitHub Actions secrets can pick up trailing whitespace or newlines when
    pasted from a clipboard, which causes HTTP-header errors (any leading or
    trailing whitespace makes the header value invalid). Always sanitise.
    """
    return os.environ.get(name, "").strip()


def build_reddit() -> praw.Reddit:
    username = _clean_env("REDDIT_USERNAME")
    # USER_AGENT secret is optional; if empty / whitespace-only, build a default.
    user_agent = _clean_env("USER_AGENT") or f"python:uk-papers-bot:v2.0 (by /u/{username})"

    reddit = praw.Reddit(
        client_id=_clean_env("REDDIT_CLIENT_ID"),
        client_secret=_clean_env("REDDIT_CLIENT_SECRET"),
        username=username,
        password=_clean_env("REDDITPASSWORD"),
        user_agent=user_agent,
        ratelimit_seconds=300,
    )
    reddit.validate_on_submit = True
    log.info("Using user agent: %r", user_agent)
    me = reddit.user.me()
    if me is None:
        raise RuntimeError("Reddit authentication returned no user — check credentials.")
    log.info("Authenticated as /u/%s", me.name)
    return reddit


def build_title(paper_name: str, paper_date: datetime) -> str:
    day_name = paper_date.strftime("%A").upper()
    date_str = paper_date.strftime("%d/%m/%Y")
    return f"{paper_name.upper()} Front Page | {day_name} {date_str}"


def recent_bot_titles(reddit: praw.Reddit, subreddit_name: str, limit: int = 50) -> set[str]:
    """Pull recent submission titles by the bot in this sub. ONE network call, used for dedupe."""
    titles: set[str] = set()
    try:
        me = reddit.user.me()
        for sub in me.submissions.new(limit=limit):
            if sub.subreddit.display_name.lower() == subreddit_name.lower():
                titles.add(sub.title)
    except PrawcoreException as e:
        log.warning("Dedup pre-fetch failed (%s) — proceeding without it.", e)
    return titles


def is_duplicate(title: str, paper_name: str, paper_date: datetime, recent: set[str]) -> bool:
    """A duplicate is an existing title that matches paper name AND paper-date day/month/year.

    We deliberately use the *paper's* date here, not 'today', because the script
    can run either side of midnight.
    """
    if title in recent:
        return True
    needle_paper = paper_name.upper()
    needle_date = paper_date.strftime("%d/%m/%Y")
    for existing in recent:
        if needle_paper in existing.upper() and needle_date in existing:
            return True
    return False


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def dismiss_cookie_banner(page: Page) -> None:
    """Sky News (like most UK news sites under PECR) shows a cookie wall on first load.

    Without dismissing, the rest of the DOM may not hydrate. Best-effort: try a few
    common selectors and move on if none match.
    """
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'button:has-text("Accept All")',
        'button:has-text("Agree")',
        'button[aria-label*="accept" i]',
        'button[title*="accept" i]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2_000)
                log.info("Dismissed cookie banner via selector: %s", sel)
                page.wait_for_timeout(1_500)
                return
        except Exception:  # noqa: BLE001
            continue
    # Sourcepoint iframe variant — try clicking inside any consent iframe.
    try:
        for frame in page.frames:
            if "sp_message" in (frame.name or "") or "consent" in (frame.url or "").lower():
                btn = frame.query_selector('button:has-text("Accept")')
                if btn:
                    btn.click(timeout=2_000)
                    log.info("Dismissed cookie banner in iframe %s", frame.url)
                    page.wait_for_timeout(1_500)
                    return
    except Exception:  # noqa: BLE001
        pass


def extract_image_url(post_handle, page_url: str) -> Optional[str]:
    """Resolve the largest available image URL from a live-blog post.

    Modern news pages lazy-load: real URL is in data-src / srcset, while src is a
    1x1 placeholder. We check all candidates and pick the largest from srcset.
    """
    img = post_handle.query_selector("img")
    if not img:
        return None

    srcset = img.get_attribute("srcset") or ""
    if srcset:
        best, best_w = None, -1
        for item in srcset.split(","):
            parts = item.strip().split()
            if not parts:
                continue
            url = parts[0]
            w = 0
            if len(parts) > 1 and parts[1].endswith("w"):
                try:
                    w = int(parts[1][:-1])
                except ValueError:
                    w = 0
            if w > best_w:
                best, best_w = url, w
        if best:
            return urljoin(page_url, best)

    for attr in ("data-src", "src"):
        val = img.get_attribute(attr)
        if val and not val.startswith("data:"):
            return urljoin(page_url, val)
    return None


def extract_paper_name_and_blurb(post_text: str) -> tuple[Optional[str], str]:
    """Pull the paper name (heading) and the body blurb from a live-blog post."""
    lines = [ln.strip() for ln in post_text.splitlines() if ln.strip()]
    if not lines:
        return None, ""

    # The first line is sometimes a "HH:MM" timestamp; skip it if so.
    idx = 0
    if re.match(r"^[0-1]?[0-9]:[0-5][0-9]$", lines[0]):
        idx = 1
    if idx >= len(lines):
        return None, ""

    paper_name = lines[idx]
    paper_name = re.sub(r"^[^\w]+", "", paper_name).rstrip(":").strip()
    if not paper_name:
        return None, ""

    blurb = "\n\n".join(lines[idx + 1:]).strip()
    return paper_name, blurb


def download_image(url: str, dest_dir: Path, safe_name: str) -> Optional[Path]:
    """Download with one retry. Reject non-image responses and tiny placeholders."""
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            resp = requests.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": BROWSER_UA},
            )
        except requests.RequestException as e:
            last_err = e
            time.sleep(1.5)
            continue

        if resp.status_code != 200:
            log.warning("HTTP %d fetching %s", resp.status_code, url)
            return None
        ctype = resp.headers.get("Content-Type", "").lower()
        if "image" not in ctype:
            log.warning("Non-image content-type %r at %s", ctype, url)
            return None
        if len(resp.content) < IMAGE_MIN_BYTES:
            log.warning("Image too small (%d bytes) at %s — likely placeholder", len(resp.content), url)
            return None

        ext = "jpg"
        if "png" in ctype:
            ext = "png"
        elif "webp" in ctype:
            ext = "webp"
        path = dest_dir / f"{safe_name}.{ext}"
        path.write_bytes(resp.content)
        return path

    log.warning("Failed to download %s: %s", url, last_err)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_env() -> None:
    required = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDITPASSWORD")
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log.error("Missing required env vars: %s", missing)
        sys.exit(2)


def open_blog(page: Page, article_id: str) -> Optional[str]:
    """Navigate to the Sky News live blog. Returns the URL actually loaded, or None."""
    for url in candidate_urls(article_id):
        log.info("Trying URL: %s", url)
        try:
            response = page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            log.warning("Timeout loading %s", url)
            continue
        if response and response.status >= 400:
            log.warning("HTTP %d at %s", response.status, url)
            continue

        dismiss_cookie_banner(page)

        try:
            page.wait_for_selector('[data-testid="live-blog-post"]', timeout=SELECTOR_TIMEOUT)
            log.info("Blog loaded: %s", page.url)
            return page.url
        except PlaywrightTimeoutError:
            log.warning("No live-blog-post selector visible at %s", url)
            continue
    return None


def main() -> int:
    validate_env()

    subreddit_name = os.environ.get("SUBREDDIT", DEFAULT_SUBREDDIT).lstrip("r/").lstrip("/") or DEFAULT_SUBREDDIT
    article_id = os.environ.get("SKY_ARTICLE_ID") or SKY_ARTICLE_ID_DEFAULT
    paper_date = expected_paper_date()

    log.info("Target subreddit : r/%s", subreddit_name)
    log.info("Expected day     : %s (%s)", expected_paper_day(), paper_date.strftime("%d/%m/%Y"))

    try:
        reddit = build_reddit()
    except (PrawcoreException, KeyError, RuntimeError) as e:
        log.error("Reddit setup failed: %s", e)
        return 2
    subreddit = reddit.subreddit(subreddit_name)
    recent_titles = recent_bot_titles(reddit, subreddit_name, limit=50)
    log.info("Loaded %d recent bot submission titles for dedupe.", len(recent_titles))

    posted_count = 0
    skipped_dupes = 0

    with tempfile.TemporaryDirectory(prefix="papers_") as tmp:
        tmp_path = Path(tmp)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=BROWSER_UA, locale="en-GB")
            page = context.new_page()

            loaded_url = open_blog(page, article_id)
            if not loaded_url:
                log.error("Could not load Sky News blog — aborting.")
                browser.close()
                return 1

            page_title = page.title()
            log.info("Page title: %s", page_title)

            posts = page.query_selector_all('[data-testid="live-blog-post"]')
            posts_to_scan = posts[:SCAN_LIMIT]
            log.info("Found %d posts total. Scanning top %d.", len(posts), len(posts_to_scan))

            for post in posts_to_scan:
                try:
                    text = post.inner_text()
                except Exception as e:  # noqa: BLE001
                    log.warning("Couldn't read post text: %s", e)
                    continue

                # End-of-coverage marker
                lower = text.lower()
                if any(phrase in lower for phrase in (
                    "that's all for today",
                    "that concludes our coverage",
                    "check back tomorrow",
                )):
                    log.info("End-of-coverage marker — stopping scan.")
                    break

                # Stale check (based on first-line timestamp only)
                post_dt = parse_post_time(text)
                if post_dt:
                    age_hours = (datetime.now(UK_TZ) - post_dt).total_seconds() / 3600
                    if age_hours > STALE_HOURS:
                        log.info("Skip — post is %.1fh old", age_hours)
                        continue

                paper_name, blurb = extract_paper_name_and_blurb(text)
                if not paper_name:
                    log.info("Skip — couldn't extract paper name.")
                    continue

                img_url = extract_image_url(post, loaded_url)
                if not img_url:
                    log.info("Skip %s — no image found.", paper_name)
                    continue

                title = build_title(paper_name, paper_date)
                if is_duplicate(title, paper_name, paper_date, recent_titles):
                    log.info("Skip %s — already posted (dedup).", paper_name)
                    skipped_dupes += 1
                    continue

                safe_name = re.sub(r"\W+", "", paper_name) or "paper"
                local = download_image(img_url, tmp_path, safe_name)
                if not local:
                    log.info("Skip %s — image download failed.", paper_name)
                    continue

                log.info("Posting: %s", title)
                try:
                    submission = subreddit.submit_image(
                        title=title,
                        image_path=str(local),
                        without_websockets=True,
                    )
                except PrawcoreException as e:
                    log.error("Reddit submit failed for %s: %s", paper_name, e)
                    continue
                except Exception as e:  # noqa: BLE001
                    log.error("Unexpected submit error for %s: %s", paper_name, e)
                    continue

                posted_count += 1
                recent_titles.add(title)

                if blurb and submission is not None:
                    try:
                        submission.reply(f"{blurb}\n\n*Via Sky News*")
                    except Exception as e:  # noqa: BLE001
                        log.warning("Reply failed for %s: %s", paper_name, e)

                time.sleep(INTER_POST_SLEEP)

            browser.close()

    log.info("Done. Posted=%d, dedup-skipped=%d", posted_count, skipped_dupes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
