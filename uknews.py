import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import urllib.parse
import difflib
import re
import hashlib
import html
import logging
import random

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- Environment variable check ---
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

# --- Reddit API credentials ---
REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
REDDIT_PASSWORD = os.environ['REDDITPASSWORD']

reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD,
    user_agent='BreakingUKNewsBot/1.0'
)
subreddit = reddit.subreddit('BreakingUKNews')

# --- Deduplication ---
def load_dedup(filename='posted_timestamps.txt'):
    posted_urls = {}
    posted_titles = {}
    posted_hashes = {}
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        parts = line.strip().split('|')
                        if len(parts) == 4:
                            timestamp, url, title, content_hash = parts
                            ts = datetime.fromisoformat(timestamp)
                            posted_urls[url] = ts
                            posted_titles[title] = ts
                            posted_hashes[content_hash] = ts
        except Exception as e:
            logger.error(f"Failed to load deduplication file: {e}")
    return posted_urls, posted_titles, posted_hashes

def save_dedup(posted_urls, posted_titles, posted_hashes, filename='posted_timestamps.txt'):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            for url, ts in posted_urls.items():
                title = next((t for t, t_ts in posted_titles.items() if t_ts == ts), "")
                ch = next((c for c, c_ts in posted_hashes.items() if c_ts == ts), "")
                f.write(f"{ts.isoformat()}|{url}|{title}|{ch}\n")
    except Exception as e:
        logger.error(f"Failed to save deduplication file: {e}")

posted_urls, posted_titles, posted_hashes = load_dedup()
now = datetime.now(timezone.utc)
cutoff = now - timedelta(days=7)
posted_urls = {k: v for k, v in posted_urls.items() if v > cutoff}
posted_titles = {k: v for k, v in posted_titles.items() if v > cutoff}
posted_hashes = {k: v for k, v in posted_hashes.items() if v > cutoff}
first_run = not bool(posted_urls)

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_content_hash(entry):
    summary = getattr(entry, "summary", "")[:200]
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def is_duplicate(entry, threshold=0.85):
    if first_run:
        return False, ""
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    if norm_link in posted_urls or content_hash in posted_hashes:
        return True, "Duplicate URL or content hash"
    for posted_title in posted_titles:
        if difflib.SequenceMatcher(None, norm_title, posted_title).ratio() > threshold:
            return True, "Title too similar to existing post"
    return False, ""

def extract_first_paragraphs(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        return '\n\n'.join(paragraphs[:3]) if paragraphs else soup.get_text(strip=True)[:500]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return f"(Could not extract article text: {e})"

def is_promotional(entry):
    combined = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in ["giveaway", "win", "offer", "sponsor", "competition", "prize", "free", "discount"])

def is_uk_relevant(entry):
    combined = (entry.title + " " + getattr(entry, "summary", "")).lower()
    keywords = ["uk", "britain", "england", "scotland", "wales", "northern ireland", "london", "nhs", "parliament", "british", "bbc", "labour", "tory", "sunak", "starmer", "met police"]
    return any(kw in combined for kw in keywords)

def get_category(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    categories = {
        "Breaking News": ["breaking", "urgent", "alert", "emergency"],
        "Politics": ["parliament", "election", "government", "policy"],
        "Crime & Legal": ["murder", "arrest", "police", "trial"],
        "Sport": ["football", "cricket", "rugby", "premier league"],
        "Royals": ["king", "queen", "royal", "prince", "princess"]
    }
    for cat, kws in categories.items():
        if any(kw in text for kw in kws):
            return cat
    return None

FLAIR_MAPPING = {
    "Breaking News": "Breaking News",
    "Politics": "Politics",
    "Crime & Legal": "Crime & Legal",
    "Sport": "Sport",
    "Royals": "Royals",
    None: "No Flair"
}

def post_to_reddit(entry, category, retries=3, base_delay=40):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    flair_text = FLAIR_MAPPING.get(category, "No Flair")
    flair_id = None
    try:
        for flair in subreddit.flair.link_templates:
            if flair['text'] == flair_text:
                flair_id = flair['id']
                break
    except Exception as e:
        logger.error(f"Failed to fetch flairs: {e}")

    for attempt in range(retries):
        try:
            submission = subreddit.submit(
                title=entry.title,
                url=entry.link,
                flair_id=flair_id
            )
            logger.info(f"Posted: {submission.shortlink}")
            body = extract_first_paragraphs(entry.link)
            if body:
                submission.reply("\n".join([f"> {line}" if line else "" for line in body.split('\n')]) + f"\n\n[Read more]({entry.link})")
            ts = datetime.now(timezone.utc)
            posted_urls[norm_link] = ts
            posted_titles[norm_title] = ts
            posted_hashes[content_hash] = ts
            save_dedup(posted_urls, posted_titles, posted_hashes)
            return True
        except praw.exceptions.RedditAPIException as e:
            if "RATELIMIT" in str(e):
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Rate limit hit, retrying in {delay}s (attempt {attempt + 1}/{retries})")
                time.sleep(delay)
            else:
                logger.error(f"Reddit API error: {e}")
                return False
        except Exception as e:
            logger.error(f"Failed to post: {e}")
            return False
    logger.error(f"Failed to post after {retries} attempts")
    return False

def main():
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "ITV": "https://www.itv.com/news/rss",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
        "Times": "https://www.thetimes.co.uk/rss"
    }

    all_entries = []
    # Randomize feed order to ensure variety
    feed_items = list(feed_sources.items())
    random.shuffle(feed_items)
    for name, url in feed_items:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if is_promotional(entry):
                    logger.info(f"Skipped promotional article: {entry.title}")
                    continue
                if not is_uk_relevant(entry):
                    logger.info(f"Skipped non-UK article: {entry.title}")
                    continue
                all_entries.append((name, entry))
        except Exception as e:
            logger.error(f"Error loading feed {name}: {e}")

    posts_made = 0
    for source, entry in all_entries:
        if posts_made >= 5:
            logger.info("Reached post limit of 5")
            break
        is_dup, reason = is_duplicate(entry)
        if is_dup:
            logger.info(f"Skipped duplicate: {entry.title} ({reason})")
            continue
        category = get_category(entry)
        success = post_to_reddit(entry, category)
        if success:
            posts_made += 1
            time.sleep(40)  # Base delay between posts
    if posts_made == 0:
        logger.warning("No new valid articles found")

if __name__ == "__main__":
    main()
