import os
import sys
import re
import time
import feedparser
import requests
import hashlib
import html
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import random
import praw
import langdetect
import pycountry
from dateutil import parser as dateparser
import difflib
import json

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s [%(levelname)s] %(message)s',
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
        user_agent='InternationalBulletinBot/1.0'
    )
    subreddit = reddit.subreddit('InternationalBulletin')
except Exception as e:
    logger.error(f"Failed to initialize Reddit API: {e}")
    sys.exit(1)

# --- Deduplication ---
DEDUP_FILE = './posted_timestamps.txt'
FUZZY_DUPLICATE_THRESHOLD = 0.40

def normalize_url(url):
    """Normalize a URL by removing trailing slashes from the path and query parameters."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))

def normalize_title(title):
    """Normalize a title by removing punctuation, collapsing spaces, and lowercasing."""
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_post_title(entry):
    """Generate a standardized post title without appending suffix."""
    return html.unescape(entry.title).strip()

def get_content_hash(entry):
    """Compute an MD5 hash of the title plus the first 300 characters of the article summary."""
    content = html.unescape(entry.title + " " + getattr(entry, "summary", "")[:300])
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def load_dedup(filename=DEDUP_FILE):
    """Load and clean deduplication data from a file, keeping only entries from the last 7 days."""
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
                            title = '|'.join(parts[2:-1])
                            hash_ = parts[-1]
                            urls.add(url)
                            titles.add(title)
                            hashes.add(hash_)
                            cleaned_lines.append(line)
                    except Exception:
                        continue
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cleaned_lines)
    logger.info(f"Loaded {len(urls)} unique entries from deduplication file (last 7 days)")
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup()

def is_duplicate(entry):
    """Check if an article is a duplicate based on URL, fuzzy title similarity, or content hash."""
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    if norm_link in posted_urls:
        return True, "Duplicate URL"
    for pt in posted_titles:
        if difflib.SequenceMatcher(None, pt, norm_title).ratio() > FUZZY_DUPLICATE_THRESHOLD:
            return True, "Duplicate Title (Fuzzy Match)"
    if content_hash in posted_hashes:
        return True, "Duplicate Content Hash"
    return False, ""

def add_to_dedup(entry):
    """Add an article to the deduplication file and in-memory sets."""
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp}|{norm_link}|{norm_title}|{content_hash}\n")
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
    """Extract exactly three paragraphs from an article URL."""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        raw_paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in raw_paragraphs:
            p_lower = p.lower()
            if ('browser' in p_lower and 'use' in p_lower) or 'view in browser' in p_lower or 'open in your browser' in p_lower or re.search(r'open (this|the) (article|page|link)', p_lower):
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', p_lower) or re.search(r'(^|\n)\s*by\s+[A-Z][\w\-\']+', p):
                continue
            if 'copyright' in p_lower or '(c)' in p_lower or '©' in p_lower or 'read our policy' in p_lower or 'external links' in p_lower or 'read more about' in p_lower:
                continue
            filtered.append(p)
            if len(filtered) >= 3:
                break
        while len(filtered) < 3:
            filtered.append("")
        return filtered[:3]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return ["", "", ""]

def get_full_article_text(url):
    """Extract the full text from an article URL by collecting all valid paragraphs."""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        raw_paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in raw_paragraphs:
            p_lower = p.lower()
            if ('browser' in p_lower and 'use' in p_lower) or 'view in browser' in p_lower or 'open in your browser' in p_lower or re.search(r'open (this|the) (article|page|link)', p_lower):
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', p_lower) or re.search(r'(^|\n)\s*by\s+[A-Z][\w\-\']+', p):
                continue
            if 'copyright' in p_lower or '(c)' in p_lower or '©' in p_lower or 'read our policy' in p_lower or 'external links' in p_lower or 'read more about' in p_lower:
                continue
            filtered.append(p)
        return ' '.join(filtered)
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch full text from URL {url}: {e}")
        return ""

# --- Filter Keywords ---
PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement",
    "sale", "deal", "black friday", "offer"
]
OPINION_KEYWORDS = [
    "opinion", "comment", "analysis", "editorial", "viewpoint", "perspective", "column"
]
IRRELEVANT_KEYWORDS = [
    "mattress", "back pain", "best mattresses", "celebrity", "gossip", "fashion", "diet",
    "workout", "product", "seasonal", "deals", "us open", "mixed doubles", "tennis tournament",
    "nfl", "nba", "super bowl", "mlb", "nhl", "oscars", "grammy", "best", "tested", "recommended"
]

EXCLUDED_KEYWORDS = [
    "gaza", "israel", "hamas", "palestine", "palestinian", "israeli",
    "west bank", "idf", "jerusalem", "hezbollah", "intifada", "netanyahu"
]

countries = [c.name.lower() for c in pycountry.countries]
international_orgs = [
    "un", "united nations", "who", "world health organization",
    "nato", "eu", "european union", "imf", "world bank",
    "wto", "g7", "g20", "asean", "opec"
]
international_terms = [
    "global", "worldwide", "international", "diplomacy",
    "foreign policy", "trade agreement", "summit", "conference",
    "bilateral", "multilateral", "treaty", "sanctions",
    "embassy", "consulate", "visa", "passport"
]

strong_international_keywords = international_terms + international_orgs + countries[:20]  # Top 20 countries for strong match

def calculate_international_relevance_score(text, url=""):
    """Calculate a relevance score for international news and return a tuple (score, matched_keywords as dict {kw: count})."""
    score = 0
    matched_keywords = {}
    text_lower = text.lower()

    # Count-based positive keywords without cap
    for keyword, weight in UK_KEYWORDS.items():  # Reuse UK_KEYWORDS for general terms, adapt weights if needed
        count = len(re.findall(r'\b' + re.escape(keyword) + r'\b', text_lower))
        if count > 0:
            score += weight * count
            matched_keywords[keyword] = count

    # Country matches
    for country in countries:
        count = len(re.findall(r'\b' + re.escape(country) + r'\b', text_lower))
        if count > 0:
            score += 2 * count  # Weight for countries
            matched_keywords[country] = count

    # International orgs
    for org in international_orgs:
        count = len(re.findall(r'\b' + re.escape(org) + r'\b', text_lower))
        if count > 0:
            score += 3 * count
            matched_keywords[org] = count

    # International terms
    for term in international_terms:
        count = len(re.findall(r'\b' + re.escape(term) + r'\b', text_lower))
        if count > 0:
            score += 1 * count
            matched_keywords[term] = count

    # Domain-based bonuses (whitelisted international sources)
    if url:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if any(d in domain for d in WHITELISTED_DOMAINS):
            score += 3
            matched_keywords["whitelisted_domain"] = 1

    return score, matched_keywords

def get_relevance_level(score, matched_keywords):
    """Return relevance level based on score."""
    has_strong_international = any(kw in matched_keywords for kw in strong_international_keywords)
    if score >= 10:
        level = "Very High"
    elif score >= 7 or has_strong_international:
        level = "High"
    elif score >= 4:
        level = "Medium"
    elif score >= 2:
        level = "Low"
    else:
        level = "Very Low"
    return level

def is_english(text):
    """Check if text is in English."""
    try:
        return langdetect.detect(text) == 'en'
    except:
        return False

def is_promotional(entry):
    """Check if an article is promotional."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)

def is_opinion(entry):
    """Check if an article is opinion-based."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in OPINION_KEYWORDS)

def is_irrelevant_fluff(entry):
    """Check if an article is irrelevant lifestyle or fluff content."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in IRRELEVANT_KEYWORDS)

def is_excluded(entry):
    """Check if article contains excluded keywords."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in EXCLUDED_KEYWORDS)

# --- Category Keywords (Adapted for International) ---
CATEGORY_KEYWORDS = {
    "Breaking News": ["breaking", "live", "update", "developing", "just in", "alert"],
    "Politics": ["politics
