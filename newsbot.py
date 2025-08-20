import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import urllib.parse
import re
import hashlib
import html
import logging
import random
from dateutil import parser as dateparser
import difflib

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Environment Variable Check ---
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME',
    'REDDITPASSWORD'
]
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# --- Reddit API Credentials ---
REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
REDDIT_PASSWORD = os.environ.get('REDDIT_PASSWORD') or os.environ.get('REDDITPASSWORD')
try:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent='BreakingUKNewsBot/1.0'
    )
    subreddit = reddit.subreddit('BreakingUKNews')
except Exception as e:
    logger.error(f"Failed to initialize Reddit API: {e}")
    sys.exit(1)

# --- Deduplication ---
DEDUP_FILE = './posted_timestamps.txt'
FUZZY_DUPLICATE_THRESHOLD = 0.88

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_post_title(entry):
    base_title = html.unescape(entry.title).strip()
    if not base_title.endswith("| UK News"):
        return f"{base_title} | UK News"
    return base_title

def get_content_hash(entry):
    content = html.unescape(entry.title + " " + getattr(entry, "summary", "")[:300])
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def load_dedup(filename=DEDUP_FILE):
    urls, titles, hashes = set(), set(), set()
    cleaned_lines = []
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4:
                    try:
                        timestamp = dateparser.parse(parts[0])
                        if timestamp > seven_days_ago:
                            url = parts[1]
                            hash = parts[-1]
                            title = '|'.join(parts[2:-1])
                            urls.add(url)
                            titles.add(title)
                            hashes.add(hash)
                            cleaned_lines.append(line)
                    except Exception:
                        continue
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cleaned_lines)
    logger.info(f"Loaded {len(urls)} unique entries from deduplication file (last 7 days)")
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup()

# In-run dedup memory
seen_urls = set()
seen_titles = set()
seen_hashes = set()

def is_duplicate(entry):
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)

    # Check persistent dedup file
    if norm_link in posted_urls:
        return True, "Duplicate URL (history)"
    for pt in posted_titles:
        if difflib.SequenceMatcher(None, pt, norm_title).ratio() > FUZZY_DUPLICATE_THRESHOLD:
            return True, "Duplicate Title (history)"
    if content_hash in posted_hashes:
        return True, "Duplicate Content Hash (history)"

    # Check in-run memory
    if norm_link in seen_urls:
        return True, "Duplicate URL (same run)"
    for st in seen_titles:
        if difflib.SequenceMatcher(None, st, norm_title).ratio() > FUZZY_DUPLICATE_THRESHOLD:
            return True, "Duplicate Title (same run)"
    if content_hash in seen_hashes:
        return True, "Duplicate Content Hash (same run)"

    return False, ""

def add_to_dedup(entry):
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}|{norm_link}|{norm_title}|{content_hash}\n")
    posted_urls.add(norm_link)
    posted_titles.add(norm_title)
    posted_hashes.add(content_hash)
    logger.info(f"Added to deduplication: {norm_title}")

    # Also mark in in-run dedup memory
    seen_urls.add(norm_link)
    seen_titles.add(norm_title)
    seen_hashes.add(content_hash)

# --- Feed Processing ---

FEEDS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.theguardian.com/uk/rss",
    "https://news.sky.com/feeds/rss/home.xml"
]


def get_entry_published_datetime(entry):
    try:
        if hasattr(entry, 'published'):
            return dateparser.parse(entry.published)
        if hasattr(entry, 'updated'):
            return dateparser.parse(entry.updated)
    except Exception:
        return None
    return None


def extract_first_paragraphs(url, max_paragraphs=2):
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = [p.get_text() for p in soup.find_all('p')]
        clean_paragraphs = [re.sub(r'\s+', ' ', p).strip() for p in paragraphs if len(p.strip()) > 40]
        return ' '.join(clean_paragraphs[:max_paragraphs]) if clean_paragraphs else None
    except Exception:
        return None


def get_full_article_text(url):
    return extract_first_paragraphs(url, max_paragraphs=3)


def is_relevant_to_uk(entry, text):
    title = entry.title.lower()
    if any(keyword in title for keyword in ['uk', 'britain', 'england', 'scotland', 'wales', 'northern ireland']):
        return True
    if text and any(keyword in text.lower() for keyword in ['uk', 'britain', 'england', 'scotland', 'wales', 'northern ireland']):
        return True
    return False


def process_feed(feed_url):
    logger.info(f"Fetching feed: {feed_url}")
    feed = feedparser.parse(feed_url)
    for entry in feed.entries:
        try:
            duplicate, reason = is_duplicate(entry)
            if duplicate:
                logger.info(f"Skipping duplicate: {entry.title} ({reason})")
                continue

            published = get_entry_published_datetime(entry)
            if not published:
                continue
            if published < datetime.now(timezone.utc) - timedelta(hours=12):
                continue

            article_text = get_full_article_text(entry.link)
            if not is_relevant_to_uk(entry, article_text):
                continue

            title = get_post_title(entry)
            logger.info(f"Posting to Reddit: {title}")
            subreddit.submit(title=title, url=entry.link)
            add_to_dedup(entry)
            time.sleep(random.randint(30, 90))
        except Exception as e:
            logger.error(f"Error processing entry {entry.get('title', 'NO TITLE')}: {e}")
            continue


def main():
    for feed_url in FEEDS:
        process_feed(feed_url)
    logger.info("Run complete.")


if __name__ == "__main__":
    main()
