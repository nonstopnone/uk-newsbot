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
    user_agent='USANewsFlashBot/1.0'
)
subreddit = reddit.subreddit('USANewsFlash')

# --- Deduplication ---
DEDUP_FILE = './posted_usanewsflash_timestamps.txt'

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
    if not base_title.endswith("| US News"):
        return f"{base_title} | US News"
    return base_title

def get_content_hash(entry):
    summary = html.unescape(getattr(entry, "summary", "")[:200])
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def load_dedup(filename=DEDUP_FILE):
    urls, titles, hashes = set(), set(), set()
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    parts = line.strip().split('|')
                    if len(parts) >= 4:
                        url = parts[1]
                        hash = parts[-1]
                        title = '|'.join(parts[2:-1])
                        urls.add(url)
                        titles.add(title)
                        hashes.add(hash)
    logger.info(f"Loaded {len(urls)} unique entries from deduplication file")
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup()

def is_duplicate(entry):
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
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = [text for text in [p.get_text(strip=True) for p in soup.find_all('p')] if len(text) > 40]
        return '\n\n'.join(paragraphs[:3]) if paragraphs else soup.get_text(strip=True)[:500]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return f"(Could not extract article text: {e})"

PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement"
]

US_KEYWORDS = {
    "washington dc": 3, "congress": 3, "senate": 3, "white house": 3, "capitol hill": 3,
    "california": 3, "texas": 3, "new york": 3, "los angeles": 3, "chicago": 3,
    "florida": 3, "boston": 3, "miami": 3, "san francisco": 3, "seattle": 3,
    "fbi": 3, "cia": 3, "pentagon": 3, "supreme court": 3, "president": 3,
    "super bowl": 3, "nfl": 3, "nba": 3, "mlb": 3, "wall street": 3,
    "united states": 2, "usa": 2, "america": 2, "american": 2,
    "democrat": 2, "republican": 2, "biden": 2, "trump": 2,
    "hollywood": 2, "silicon valley": 2, "broadway": 2,
    "nasa": 2, "cdc": 2, "fda": 2,
    "government": 1, "economy": 1, "policy": 1, "election": 1, "inflation": 1,
    "federal": 1, "state": 1, "county": 1, "city": 1
}

NEGATIVE_KEYWORDS = {
    "london": -2, "parliament": -2, "brexit": -2, "nhs": -2, "bbc": -2,
    "sky news": -2, "itv": -2, "telegraph": -2, "times": -2,
    "king charles": -2, "queen camilla": -2, "prince william": -2,
    "princess kate": -2, "downing street": -2, "buckingham palace": -2,
    "manchester": -2, "birmingham": -2, "glasgow": -2, "edinburgh": -2,
    "france": -1, "germany": -1, "china": -1, "russia": -1, "india": -1,
    "australia": -1, "canada": -1, "japan": -1, "brazil": -1, "south africa": -1
}

def calculate_us_relevance_score(text):
    score = 0
    text_lower = text.lower()
    for keyword, weight in US_KEYWORDS.items():
        if keyword in text_lower:
            score += weight
    for keyword, weight in NEGATIVE_KEYWORDS.items():
        if keyword in text_lower:
            score += weight
    return score

def is_promotional(entry):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    if "offer" in combined and any(kw in combined for kw in ["government", "policy", "public sector"]):
        return False
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)

def is_us_relevant(entry, threshold=2):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    score = calculate_us_relevance_score(combined)
    print(f"Article: {html.unescape(entry.title)} | Relevance Score: {score}")
    if score < threshold:
        logger.info(f"Filtered out article with score {score}: {html.unescape(entry.title)}")
    return score >= threshold

CATEGORY_KEYWORDS = {
    "Breaking News": ["breaking", "live", "update", "developing", "just in", "alert"],
    "Politics": ["politics", "congress", "senate", "government", "election", "policy", "president", "governor"],
    "Crime & Legal": ["crime", "police", "court", "legal", "arrest", "trial", "investigation", "prosecution"],
    "Sports": ["sport", "football", "basketball", "baseball", "socCER", "nfl", "nba", "mlb", "match", "game"],
    "Entertainment": ["entertainment", "hollywood", "celebrity", "movie", "tv show", "music", "award", "oscar"],
    "Royals": ["king", "queen", "prince", "princess", "royal family", "monarchy", "buckingham palace", "windsor"]
}

def get_category(entry):
    text = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    if "Royals" in CATEGORY_KEYWORDS:
        royals_keywords = CATEGORY_KEYWORDS['Royals']
        if any(re.search(r'\b' + re.escape(kw) + r'\b', text) for kw in royals_keywords) and not any(name in text for name in ["meghan markle", "prince harry"]):
            return "Royals"
    for cat in ["Politics", "Crime & Legal", "Sports", "Entertainment"]:
        for keyword in CATEGORY_KEYWORDS[cat]:
            if re.search(r'\b' + re.escape(keyword) + r'\b', text):
                return cat
    return "Breaking News"

FLAIR_MAPPING = {
    "Breaking News": "Breaking News",
    "Politics": "Politics",
    "Crime & Legal": "Crime & Legal",
    "Sports": "Sports",
    "Entertainment": "Entertainment",
    "Royals": "Royals",
    None: "No Flair"
}

def post_to_reddit(entry, category, retries=3, base_delay=40):
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
            add_to_dedup(entry)
            body = extract_first_paragraphs(entry.link)
            if body:
                reply_text = "\n".join([f"> {html.unescape(line)}" if line else "" for line in body.split('\n')])
                submission.reply(reply_text + f"\n\n[Read more]({entry.link})")
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
        "CNN": "https://rss.cnn.com/rss/cnn_topstories.rss",
        "Fox News": "https://moxie.foxnews.com/google-publisher/latest.xml",
        "NBC News": "https://feeds.nbcnews.com/nbcnews/public/news",
        "ABC News": "https://abcnews.go.com/abcnews/topstories",
        "NY Times": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "BBC US": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"
    }

    articles_by_source = {source: [] for source in feed_sources}
    now = datetime.now(timezone.utc)
    three_hours_ago = now - timedelta(hours=3)
    feed_items = list(feed_sources.items())
    random.shuffle(feed_items)

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
                if not is_us_relevant(entry):
                    logger.info(f"Skipped non-US article: {html.unescape(entry.title)}")
                    continue
                articles_by_source[name].append(entry)
        except Exception as e:
            logger.error(f"Error loading feed {name}: {e}")

    # Post collected articles
    for source, articles in articles_by_source.items():
        for entry in articles:
            is_dup, reason = is_duplicate(entry)
            if is_dup:
                logger.info(f"Skipped duplicate article: {html.unescape(entry.title)} ({reason})")
                continue
            category = get_category(entry)
            if post_to_reddit(entry, category):
                logger.info(f"Successfully posted: {html.unescape(entry.title)}")
            else:
                logger.error(f"Failed to post: {html.unescape(entry.title)}")

if __name__ == "__main__":
    main()
