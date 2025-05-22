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

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
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
DEDUP_FILE = 'posted_timestamps.txt'

def normalize_url(url):
    """Normalize a URL by removing trailing slashes from the path."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def normalize_title(title):
    """Normalize a title by removing punctuation (except £$€), collapsing spaces, and lowercasing."""
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_post_title(entry):
    """Generate a standardized post title, appending ' | UK News' if not present."""
    base_title = html.unescape(entry.title).strip()
    if not base_title.endswith("| UK News"):
        return f"{base_title} | UK News"
    return base_title

def get_content_hash(entry):
    """Compute an MD5 hash of the first 200 characters of the article summary."""
    summary = html.unescape(getattr(entry, "summary", "")[:200])
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def load_dedup(filename=DEDUP_FILE):
    """
    Load deduplication data from file into sets.
    Handles cases where titles might contain '|' by reconstructing the title from parts.
    """
    urls, titles, hashes = set(), set(), set()
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    parts = line.strip().split('|')
                    if len(parts) >= 4:
                        timestamp = parts[0]
                        url = parts[1]
                        hash = parts[-1]
                        title = '|'.join(parts[2:-1])  # Reconstruct title
                        urls.add(url)
                        titles.add(title)
                        hashes.add(hash)
    logger.info(f"Loaded {len(urls)} unique entries from deduplication file")
    return urls, titles, hashes

# Initialize global deduplication sets
posted_urls, posted_titles, posted_hashes = load_dedup()

def is_duplicate(entry):
    """Check if an article is a duplicate based on URL, title, or content hash."""
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    if norm_link in posted_urls:
        return True, "Duplicate URL"
    if norm_title in posted_titles:
        return True, "Duplicate Title"
    if content_hash in posted_hashes:
        return True, "Duplicate Content Hash"
    return False, ""

def add_to_dedup(entry):
    """Add an article to the deduplication file and in-memory sets by appending a new line."""
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    # Append to the file
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}|{norm_link}|{norm_title}|{content_hash}\n")
    # Update in-memory sets
    posted_urls.add(norm_link)
    posted_titles.add(norm_title)
    posted_hashes.add(content_hash)
    logger.info(f"Added to deduplication: {norm_title}")

def get_entry_published_datetime(entry):
    """Extract the publication datetime from an RSS entry, defaulting to UTC if no timezone."""
    for field in ['published', 'updated', 'created', 'date']:
        if hasattr(entry, field):
            try:
                dt = dateparser.parse(getattr(entry, field))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    return None

def extract_first_paragraphs(url):
    """Extract the first three paragraphs from an article URL, or a snippet if that fails."""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        return '\n\n'.join(paragraphs[:3]) if paragraphs else soup.get_text(strip=True)[:500]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return f"(Could not extract article text: {e})"

# --- Filter Keywords (Updated for 2025) ---
PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "offer", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement"
]

UK_RELEVANT_KEYWORDS = [
    "uk", "britain", "united kingdom", "england", "scotland", "wales", "northern ireland", "london",
    "nhs", "parliament", "westminster", "downing street", "no 10", "no. 10", "whitehall",
    "british", "labour", "conservative", "lib dem", "liberal democrat", "snp", "green party",
    "kemi badenoch", "rachel reeves", "keir starmer", "ed davey", "john swinney", "carla denyer", "adrian ramsay",
    "bbc", "itv", "sky news", "met police", "scotland yard", "mi5", "mi6",
    "king charles", "queen camilla", "prince william", "princess kate", "prince george", "princess charlotte",
    "ofgem", "bank of england", "inflation", "cost of living", "energy price cap"
]

CATEGORIES = {
    "Breaking News": ["breaking", "urgent", "alert", "emergency"],
    "Politics": [
        "parliament", "election", "government", "policy", "prime minister", "chancellor", "cabinet",
        "kemi badenoch", "keir starmer", "rachel reeves", "ed davey", "john swinney"
    ],
    "Crime & Legal": [
        "murder", "arrest", "police", "trial", "court", "sentencing", "investigation", "scotland yard", "met police"
    ],
    "Sport": [
        "football", "cricket", "rugby", "premier league", "wimbledon", "six nations", "fa cup", "england squad"
    ],
    "Royals": [
        "king charles", "queen camilla", "prince william", "princess kate", "royal", "prince", "princess"
    ]
}

def is_promotional(entry):
    """Check if an article is promotional based on keywords."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)

def is_uk_relevant(entry):
    """Check if an article is UK-relevant based on keywords."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in UK_RELEVANT_KEYWORDS)

def get_category(entry):
    """Determine the category of an article based on keywords."""
    text = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    for cat, kws in CATEGORIES.items():
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
    """Post an article to Reddit with flair and a comment containing the first few paragraphs."""
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
            post_title = get_post_title(entry)
            submission = subreddit.submit(
                title=post_title,
                url=entry.link,
                flair_id=flair_id
            )
            logger.info(f"Posted: {submission.shortlink}")
            body = extract_first_paragraphs(entry.link)
            if body:
                reply_text = "\n".join([f"> {html.unescape(line)}" if line else "" for line in body.split('\n')])
                submission.reply(reply_text + f"\n\n[Read more]({entry.link})")
            add_to_dedup(entry)
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
    """Main function to fetch RSS feeds, filter articles, and post to Reddit."""
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "ITV": "https://www.itv.com/news/rss",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
        "Times": "https://www.thetimes.co.uk/rss"
    }
    all_entries = []
    feed_items = list(feed_sources.items())
    random.shuffle(feed_items)
    now = datetime.now(timezone.utc)
    three_hours_ago = now - timedelta(hours=3)

    for name, url in feed_items:
        try:
            feed = feedparser.parse(url)
            entries = list(feed.entries)
            random.shuffle(entries)
            for entry in entries:
                published_dt = get_entry_published_datetime(entry)
                if not published_dt or published_dt < three_hours_ago or published_dt > now + timedelta(minutes=5):
                    continue
                if is_promotional(entry):
                    logger.info(f"Skipped promotional article: {html.unescape(entry.title)}")
                    continue
                if not is_uk_relevant(entry):
                    logger.info(f"Skipped non-UK article: {html.unescape(entry.title)}")
                    continue
                all_entries.append((name, entry))
        except Exception as e:
            logger.error(f"Error loading feed {name}: {e}")

    random.shuffle(all_entries)

    posts_made = 0
    for source, entry in all_entries:
        if posts_made >= 5:
            logger.info("Reached post limit of 5")
            break
        is_dup, reason = is_duplicate(entry)
        if is_dup:
            logger.info(f"Skipped duplicate: {html.unescape(entry.title)} ({reason})")
            continue
        category = get_category(entry)
        success = post_to_reddit(entry, category)
        if success:
            posts_made += 1
            time.sleep(40)
    if posts_made == 0:
        logger.warning("No new valid articles found")

if __name__ == "__main__":
    main()
