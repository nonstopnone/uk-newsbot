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

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Environment Variable Check
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

# Reddit API Credentials
REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
REDDIT_PASSWORD = os.environ['REDDITPASSWORD']

reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD,
    user_agent='UKRoyalNewsBot/1.0'
)
subreddit = reddit.subreddit('UKRoyalNews')

# Deduplication
DEDUP_FILE = './posted_timestamps.txt'

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
    """Generate a standardized post title, appending ' | UK Royal News' if not present."""
    base_title = html.unescape(entry.title).strip()
    if not base_title.endswith("| UK Royal News"):
        return f"{base_title} | UK Royal News"
    return base_title

def get_content_hash(entry):
    """Compute an MD5 hash of the first 200 characters of the article summary."""
    summary = html.unescape(getattr(entry, "summary", "")[:200])
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def load_dedup(filename=DEDUP_FILE):
    """Load deduplication data from file into sets."""
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
                        title = '|'.join(parts[2:-1])
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
    """Add an article to the deduplication file and in-memory sets."""
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
    """Extract the first three paragraphs from an article URL."""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        return '\n\n'.join(paragraphs[:3]) if paragraphs else soup.get_text(strip=True)[:500]
 Iran requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return f"(Could not extract article text: {e})"

# Filter Keywords
PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement"
]

ROYAL_KEYWORDS = {
    # High weight (3) - Strongly indicative of royal context
    "king charles": 3, "queen camilla": 3, "prince william": 3, "princess kate": 3,
    "prince harry": 3, "prince george": 3, "princess charlotte": 3,
    "prince louis": 3, "buckingham palace": 3, "windsor castle": 3, "sandringham": 3,
    "balmoral": 3, "royal family": 3, "monarchy": 3, "royal": 3, "duke of edinburgh": 3,
    "princess anne": 3, "prince edward": 3, "duchess of york": 3, "royal ascot": 3,
    "trooping the colour": 3, "commonwealth": 3, "royal tour": 3, "royal patron": 3,
    # Medium weight (2) - General royal-related terms
    "prince": 2, "princess": 2, "duke": 2, "duchess": 2, "queen": 2, "king": 2,
    "royal event": 2, "royal engagement": 2, "royal charity": 2, "royal residence": 2,
    # Low weight (1) - Broad terms that may appear in royal context
    "ceremony": 1, "palace": 1, "crown": 1, "throne": 1, "royal visit": 1
}

NEGATIVE_KEYWORDS = {
    # High negative weight (-2) - Strongly indicative of non-UK/royal context
    "washington dc": -2, "congress": -2, "senate": -2, "white house": -2,
    "california": -2, "texas": -2, "new york": -2, "los angeles": -2,
    "fbi": -2, "cia": -2, "pentagon": -2, "biden": -2, "trump": -2,
    # Medium negative weight (-1) - General international terms
    "france": -1, "germany": -1, "china": -1, "russia": -1, "india": -1,
    "australia": -1, "canada": -1, "japan": -1, "brazil": -1, "south africa": -1
}

def calculate_royal_relevance_score(text):
    """Calculate a relevance score for royal-related content."""
    score = 0
    text_lower = text.lower()
    for keyword, weight in ROYAL_KEYWORDS.items():
        if keyword in text_lower:
            score += weight
    for keyword, weight in NEGATIVE_KEYWORDS.items():
        if keyword in text_lower:
            score += weight  # weight is negative
    return score

def is_promotional(entry):
    """Check if an article is promotional, allowing 'offer' in royal/charity contexts."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    if "offer" in combined:
        if any(kw in combined for kw in ["charity", "patron", "royal event", "royal engagement"]):
            return False
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)

def is_royal_relevant(entry, threshold=3):
    """Check if an article is royal-relevant, excluding Meghan Markle mentions."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    excluded_terms = ["meghan markle", "duchess of sussex", "meghan, duchess of sussex"]
    if any(term in combined for term in excluded_terms):
        logger.info(f"Excluded article mentioning Meghan Markle: {html.unescape(entry.title)}")
        return False
    score = calculate_royal_relevance_score(combined)
    print(f"Article: {html.unescape(entry.title)} | Royal Relevance Score: {score}")
    if score < threshold:
        logger.info(f"Filtered out non-royal article with score {score}: {html.unescape(entry.title)}")
    return score >= threshold

def post_to_reddit(entry, retries=3, base_delay=40):
    """Post an article to Reddit with a comment."""
    for attempt in range(retries):
        try:
            post_title = get_post_title(entry)
            submission = subreddit.submit(
                title=post_title,
                url=entry.link
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
    """Fetch RSS feeds until 3 unique royal-related articles are found and posted."""
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "ITV": "https://www.itv.com/news/rss",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
        "Times": "https://www.thetimes.co.uk/rss"
    }
    selected_articles = []
    posts_made = 0
    time_window_hours = 3
    max_time_window_hours = 48
    now = datetime.now(timezone.utc)

    while posts_made < 3 and time_window_hours <= max_time_window_hours:
        earliest_time = now - timedelta(hours=time_window_hours)
        logger.info(f"Searching for articles published after {earliest_time.isoformat()}")

        feed_items = list(feed_sources.items())
        random.shuffle(feed_items)

        for name, url in feed_items:
            if posts_made >= 3:
                break
            try:
                feed = feedparser.parse(url)
                entries = list(feed.entries)
                random.shuffle(entries)
                for entry in entries:
                    if posts_made >= 3:
                        break
                    published_dt = get_entry_published_datetime(entry)
                    if not published_dt or published_dt < earliest_time or published_dt > now + timedelta(minutes=5):
                        continue
                    if is_promotional(entry):
                        logger.info(f"Skipped promotional article: {html.unescape(entry.title)}")
                        continue
                    if not is_royal_relevant(entry):
                        continue
                    is_dup, reason = is_duplicate(entry)
                    if is_dup:
                        logger.info(f"Skipped duplicate article: {html.unescape(entry.title)} - {reason}")
                        continue
                    selected_articles.append((name, entry))
                    posts_made += 1
            except Exception as e:
                logger.error(f"Error loading feed {name}: {e}")

        if posts_made < 3:
            time_window_hours += 3
            logger.info(f"Expanding time window to {time_window_hours} hours")

    for source, entry in selected_articles:
        success = post_to_reddit(entry)
        if success:
            logger.info(f"Posted from {source}: {html.unescape(entry.title)}")
            time.sleep(40)
        else:
            posts_made -= 1
            logger.error(f"Failed to post article from {source}: {html.unescape(entry.title)}")

    if posts_made < 3:
        logger.warning(f"Could only post {posts_made} articles; not enough unique, royal-related stories found")
    else:
        logger.info(f"Successfully posted {posts_made} articles")

if __name__ == "__main__":
    main()
