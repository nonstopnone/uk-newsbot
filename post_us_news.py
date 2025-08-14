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

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Environment variable check
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME',
    'REDDITPASSWORD'  # Reverted: Changed back to 'REDDITPASSWORD' without underscore as per user instruction
]
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# Reddit API setup
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    username=os.environ['REDDIT_USERNAME'],
    password=os.environ['REDDITPASSWORD'],  # Reverted: Changed back to 'REDDITPASSWORD' without underscore
    user_agent='USANewsFlashBot/1.0'
)
subreddit = reddit.subreddit('USANewsFlash')

# Deduplication file
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

def get_summary_text(entry):
    summary = getattr(entry, "summary", "")
    if summary:
        soup = BeautifulSoup(summary, 'html.parser')
        return soup.get_text(strip=True)
    return ""

PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement"
]

# Modified: Expanded US_KEYWORDS significantly with more states, cities, institutions, politicians, and events for better relevance coverage
# Added keywords for disasters, shootings, and crimes with higher weights to ensure they are always considered relevant when combined with US context
US_KEYWORDS = {
    "washington dc": 3, "congress": 3, "senate": 3, "white house": 3, "capitol hill": 3,
    "california": 3, "texas": 3, "new york": 3, "los angeles": 3, "chicago": 3,
    "florida": 3, "boston": 3, "miami": 3, "san francisco": 3, "seattle": 3,
    "alabama": 3, "alaska": 3, "arizona": 3, "arkansas": 3, "colorado": 3,
    "connecticut": 3, "delaware": 3, "georgia": 3, "hawaii": 3, "idaho": 3,
    "illinois": 3, "indiana": 3, "iowa": 3, "kansas": 3, "kentucky": 3,
    "louisiana": 3, "maine": 3, "maryland": 3, "massachusetts": 3, "michigan": 3,
    "minnesota": 3, "mississippi": 3, "missouri": 3, "montana": 3, "nebraska": 3,
    "nevada": 3, "new hampshire": 3, "new jersey": 3, "new mexico": 3, "north carolina": 3,
    "north dakota": 3, "ohio": 3, "oklahoma": 3, "oregon": 3, "pennsylvania": 3,
    "rhode island": 3, "south carolina": 3, "south dakota": 3, "tennessee": 3, "utah": 3,
    "vermont": 3, "virginia": 3, "washington": 3, "west virginia": 3, "wisconsin": 3, "wyoming": 3,
    "houston": 3, "philadelphia": 3, "phoenix": 3, "san antonio": 3, "san diego": 3,
    "dallas": 3, "san jose": 3, "austin": 3, "jacksonville": 3, "fort worth": 3,
    "columbus": 3, "charlotte": 3, "indianapolis": 3, "denver": 3, "detroit": 3,
    "fbi": 3, "cia": 3, "pentagon": 3, "supreme court": 3, "president": 3,
    "vice president": 3, "house of representatives": 3, "department of justice": 3, "irs": 3, "epa": 3,
    "super bowl": 3, "nfl": 3, "nba": 3, "mlb": 3, "wall street": 3,
    "united states": 2, "usa": 2, "america": 2, "american": 2,
    "democrat": 2, "republican": 2, "biden": 2, "trump": 2, "harris": 2, "obama": 2,
    "hollywood": 2, "silicon valley": 2, "broadway": 2,
    "nasa": 2, "cdc": 2, "fda": 2, "nih": 2, "nsa": 2,
    "government": 1, "economy": 1, "policy": 1, "election": 1, "inflation": 1,
    "federal": 1, "state": 1, "county": 1, "city": 1,
    # Added: Keywords for disasters, shootings, and crimes with weights to prioritize reporting
    "shooting": 3, "school shooting": 4, "mass shooting": 4, "gun violence": 3,
    "crime": 3, "murder": 3, "robbery": 3, "assault": 3,
    "disaster": 3, "hurricane": 3, "tornado": 3, "earthquake": 3, "flood": 3,
    "wildfire": 3, "blizzard": 3, "drought": 3
}

# Modified: Added more negative keywords for Canada to better distinguish US from Canada in BBC US & Canada feed
NEGATIVE_KEYWORDS = {
    "london": -2, "parliament": -2, "brexit": -2, "nhs": -2, "bbc": -2,
    "sky news": -2, "itv": -2, "telegraph": -2, "times": -2,
    "king charles": -2, "queen camilla": -2, "prince william": -2,
    "princess kate": -2, "downing street": -2, "buckingham palace": -2,
    "manchester": -2, "birmingham": -2, "glasgow": -2, "edinburgh": -2,
    "france": -1, "germany": -1, "china": -1, "russia": -1, "india": -1,
    "australia": -1, "canada": -2, "japan": -1, "brazil": -1, "south africa": -1,
    # Added: Canada-specific negatives
    "toronto": -2, "vancouver": -2, "ottawa": -2, "montreal": -2, "calgary": -2,
    "quebec": -2, "ontario": -2, "british columbia": -2, "trudeau": -2
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

# Added: New function to filter out opinion pieces, especially from NYT, and avoid Trump opinion pieces
def is_opinion_piece(entry, name):
    title_lower = html.unescape(entry.title).lower()
    link_lower = entry.link.lower()
    if name == "NY Times":
        if "/opinion/" in link_lower or "opinion" in title_lower:
            logger.info(f"Filtered out opinion piece: {html.unescape(entry.title)}")
            return True
        # Specifically avoid Trump political opinions
        if "trump" in title_lower and ("opinion" in title_lower or "op-ed" in title_lower or "editorial" in title_lower):
            logger.info(f"Filtered out Trump opinion piece: {html.unescape(entry.title)}")
            return True
    # General opinion filter for other sources if applicable
    if "opinion" in title_lower or "op-ed" in title_lower or "editorial" in title_lower:
        logger.info(f"Filtered out general opinion piece: {html.unescape(entry.title)}")
        return True
    return False

def is_clickbait(entry):
    title = html.unescape(entry.title).lower()
    clickbait_keywords = [
        "shocking", "you won't believe", "insane", "crazy", "epic", "fail", "win",
        "top 10", "must see", "viral", "bombshell", "explosive", "outrageous",
        "unbelievable", "mind-blowing"
    ]
    if any(kw in title for kw in clickbait_keywords):
        return True
    # Additional check for vague "this/that" questions or ellipses
    if ("this" in title or "that" in title) and (title.endswith("?") or title.endswith("...")):
        return True
    return False

# Modified: Adjusted threshold dynamically: lower (1) for BBC US to accept most stories as highly suitable, higher (3) for others for stricter US focus
# This ensures BBC unbiased news takes primacy and most of their stories are posted
def is_us_relevant(entry, name):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    score = calculate_us_relevance_score(combined)
    threshold = 1 if name == "BBC US" else 3
    if score < threshold:
        logger.info(f"Filtered out article with score {score} (threshold {threshold}): {html.unescape(entry.title)}")
    return score >= threshold, score

CATEGORY_KEYWORDS = {
    "Breaking News": ["breaking", "live", "update", "developing", "just in", "alert"],
    "Politics": ["politics", "congress", "senate", "government", "election", "policy", "president", "governor"],
    "Crime & Legal": ["crime", "police", "court", "legal", "arrest", "trial", "investigation", "prosecution"],
    "Sports": ["sport", "football", "basketball", "baseball", "soccer", "nfl", "nba", "mlb", "match", "game"],
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
            body = get_summary_text(entry)
            if body:
                submission.reply(f"{body}\n\n[Read more]({entry.link})")
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
        "NBC News": "https://feeds.nbcnews.com/nbcnews/public/news",
        "ABC News": "https://abcnews.go.com/abcnews/topstories",
        "NY Times": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "BBC US": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"
    }

    eligible_articles = []
    now = datetime.now(timezone.utc)
    three_hours_ago = now - timedelta(hours=3)
    feed_items = list(feed_sources.items())
    random.shuffle(feed_items)

    for name, url in feed_items:
        try:
            feed = feedparser.parse(url)
            entries = list(feed.entries)
            random.shuffle(entries)  # Preserve: Random shuffle to avoid source bias in initial collection
            for entry in entries:
                published_dt = get_entry_published_datetime(entry)
                if not published_dt or published_dt < three_hours_ago or published_dt > now + timedelta(minutes=5):
                    continue
                if is_promotional(entry):
                    continue
                if is_clickbait(entry):
                    logger.info(f"Filtered out clickbait: {html.unescape(entry.title)}")
                    continue
                # Added: Filter opinion pieces before relevance check
                if is_opinion_piece(entry, name):
                    continue
                # Modified: Pass name to is_us_relevant for dynamic threshold
                relevant, score = is_us_relevant(entry, name)
                if not relevant:
                    continue
                is_dup, reason = is_duplicate(entry)
                if is_dup:
                    continue
                eligible_articles.append((name, entry, score))
        except Exception as e:
            logger.error(f"Error loading feed {name}: {e}")

    # Preserve: Prioritize BBC by sorting and selecting top BBC first, then others, based on score
    bbc_eligible = [art for art in eligible_articles if art[0] == "BBC US"]
    others_eligible = [art for art in eligible_articles if art[0] != "BBC US"]
    bbc_eligible.sort(key=lambda x: x[2], reverse=True)
    others_eligible.sort(key=lambda x: x[2], reverse=True)
    selected_articles = bbc_eligible[:5]
    if len(selected_articles) < 5:
        selected_articles += others_eligible[:5 - len(selected_articles)]

    if len(selected_articles) < 5:
        logger.warning(f"Only found {len(selected_articles)} eligible articles, proceeding with available ones")

    for name, entry, score in selected_articles:
        category = get_category(entry)
        if post_to_reddit(entry, category):
            logger.info(f"Successfully posted: {html.unescape(entry.title)}")
        else:
            logger.error(f"Failed to post: {html.unescape(entry.title)}")

if __name__ == "__main__":
    main()
