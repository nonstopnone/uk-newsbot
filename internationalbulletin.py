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

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Environment Variable Check ---
required_env_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME', 'REDDITPASSWORD']
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# --- Reddit API Setup ---
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    username=os.environ['REDDIT_USERNAME'],
    password=os.environ['REDDITPASSWORD'],
    user_agent='InternationalBulletinBot/1.0'
)
subreddit = reddit.subreddit('InternationalBulletin')

# --- Deduplication File Setup ---
for fname in ['posted_urls.txt', 'posted_titles.txt', 'posted_content_hashes.txt']:
    if not os.path.exists(fname):
        with open(fname, 'w', encoding='utf-8'):
            pass

# --- RSS Feeds for International News ---
feed_sources = {
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters World": "http://feeds.reuters.com/Reuters/worldNews",
    "CNN International": "http://rss.cnn.com/rss/edition_world.rss",
    "AP News": "https://www.apnews.com/hub/apnewsfeed",
    "New York Times": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "Washington Post": "https://feeds.washingtonpost.com/rss/world",
    "Deutsche Welle": "https://rss.dw.com/rdf/rss/en/all",
    "France 24": "https://www.france24.com/en/rss"
}

# --- Deduplication Helpers ---
def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme + "://" + parsed.netloc + parsed.path.rstrip('/')

def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_content_hash(entry):
    summary = html.unescape(getattr(entry, "summary", "")[:200])
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def load_posted(fname):
    d = {}
    if os.path.exists(fname):
        with open(fname, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or '|' not in line:
                    continue
                value, timestamp = line.split('|', 1)
                try:
                    d[value] = datetime.fromisoformat(timestamp)
                except Exception:
                    continue
    return d

posted_urls = load_posted('posted_urls.txt')
posted_titles = load_posted('posted_titles.txt')
posted_content_hashes = load_posted('posted_content_hashes.txt')

def is_duplicate(entry):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    now = datetime.now(timezone.utc)
    threshold = timedelta(days=7)
    if norm_link in posted_urls and (now - posted_urls[norm_link]) < threshold:
        return True, "Duplicate URL"
    if norm_title in posted_titles and (now - posted_titles[norm_title]) < threshold:
        return True, "Duplicate Title"
    if content_hash in posted_content_hashes and (now - posted_content_hashes[content_hash]) < threshold:
        return True, "Duplicate Content Hash"
    return False, ""

def add_to_dedup(entry):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    now = datetime.now(timezone.utc)
    posted_urls[norm_link] = now
    posted_titles[norm_title] = now
    posted_content_hashes[content_hash] = now
    save_duplicates()

def save_duplicates():
    for fname, container in [
        ('posted_urls.txt', posted_urls),
        ('posted_titles.txt', posted_titles),
        ('posted_content_hashes.txt', posted_content_hashes)
    ]:
        try:
            with open(fname, 'w', encoding='utf-8') as f:
                for key, timestamp in container.items():
                    f.write(f"{key}|{timestamp.isoformat()}\n")
        except Exception as e:
            logger.error(f"Failed to save {fname}: {e}")

# --- Article Quality Control ---
unwanted_patterns = [
    r"author", r"byline", r"written by", r"support us", r"subscribe", r"view in",
    r"click here", r"read more", r"advertisement", r"sponsored",
    r"http", r"www", r"\.com", r"@", r"^[A-Z\s]+$"
]

def is_good_paragraph(text):
    if len(text) < 50:
        return False
    text_lower = text.lower()
    for pattern in unwanted_patterns:
        if re.search(pattern, text_lower):
            return False
    return True

def extract_first_paragraphs(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = [p.get_text().strip() for p in soup.find_all('p') if p.get_text().strip()]
        good_paragraphs = []
        for p in paragraphs:
            if is_good_paragraph(p):
                good_paragraphs.append(p)
            if len(good_paragraphs) == 3:
                break
        return '\n\n'.join(good_paragraphs)
    except Exception as e:
        logger.warning(f"Failed to extract paragraphs from {url}: {e}")
        return ""

# --- Language and Promotional Checks ---
PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "promotion", "contest", "advert", "sponsor",
    "deal", "offer", "competition", "prize", "free", "discount"
]

def is_english(text):
    try:
        return langdetect.detect(text) == 'en'
    except:
        return False

def is_promotional(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in PROMOTIONAL_KEYWORDS)

# --- Relevance Scoring for International News ---
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

def calculate_international_relevance_score(text):
    text_lower = text.lower()
    # Count unique countries
    mentioned_countries = set(country for country in countries if country in text_lower)
    num_countries = len(mentioned_countries)
    country_score = num_countries + (3 if num_countries >= 2 else 0)  # Bonus for multiple countries
    # Count international organizations
    org_score = sum(1 for org in international_orgs if org in text_lower)
    # Count general international terms
    term_score = sum(1 for term in international_terms if term in text_lower)
    return country_score + org_score + term_score

# --- Helper Function to Extract Country Name ---
def get_country_name(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    for country in countries:
        if country in text:
            return country.title()  # Capitalize country name
    return "International Bulletin"  # Fallback

# --- Helper Function ---
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

# --- Main Logic ---
MAX_POSTS_PER_RUN = 10
HOURS_THRESHOLD = 12
now_utc = datetime.now(timezone.utc)
hours_ago = now_utc - timedelta(hours=HOURS_THRESHOLD)

logger.info("Starting international news posting process...")
candidates = []
feed_sources_items = list(feed_sources.items())
random.shuffle(feed_sources_items)

for source, feed_url in feed_sources_items:
    logger.info(f"Fetching feed: {source} ({feed_url})")
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            logger.warning(f"No entries found for {source}")
            continue
        random.shuffle(feed.entries)
        for entry in feed.entries:
            pubdate = get_entry_published_datetime(entry)
            if not pubdate or pubdate < hours_ago:
                logger.info(f"Skipping old article: {entry.title}")
                continue
            summary = getattr(entry, "summary", "")
            text_for_lang = summary if summary else entry.title
            if not is_english(text_for_lang):
                logger.info(f"Skipping non-English article: {entry.title}")
                continue
            if is_promotional(entry):
                logger.info(f"Skipping promotional article: {entry.title}")
                continue
            is_dup, reason = is_duplicate(entry)
            if is_dup:
                logger.info(f"Skipping duplicate article: {entry.title} ({reason})")
                continue
            text = entry.title + " " + summary
            relevance_score = calculate_international_relevance_score(text)
            age_in_hours = (now_utc - pubdate).total_seconds() / 3600
            total_score = relevance_score + (HOURS_THRESHOLD - age_in_hours)
            candidates.append((source, entry, total_score))
            logger.info(f"Candidate: {entry.title} | Relevance Score: {relevance_score:.2f} | Age: {age_in_hours:.2f} hours | Total Score: {total_score:.2f}")
    except Exception as e:
        logger.error(f"Failed to parse feed {source}: {e}")

if not candidates:
    logger.info("No candidates found.")
    sys.exit(0)

logger.info(f"Collected {len(candidates)} candidate articles.")
# Sort by total score (relevance + recency) and select top 10
candidates.sort(key=lambda x: x[2], reverse=True)
selected_entries = candidates[:MAX_POSTS_PER_RUN]
logger.info(f"Selected {len(selected_entries)} articles to post:")
for i, (source, entry, score) in enumerate(selected_entries, 1):
    logger.info(f"{i}. {entry.title} from {source} | Total Score: {score:.2f}")

current_posts = []
for source, entry, score in selected_entries:
    try:
        logger.info(f"Processing article: {entry.title}")
        country_name = get_country_name(entry)  # Extract country name or use fallback
        post_title = f"{html.unescape(entry.title)} | {country_name} News"
        submission = subreddit.submit(title=post_title, url=entry.link)
        logger.info(f"Posted Title Headline: {post_title}")
        logger.info(f"Reddit Link: {submission.shortlink} | Article URL: {entry.link}")
        paragraphs = extract_first_paragraphs(entry.link)
        if paragraphs:
            comment_text = "\n\n".join(f"> {line}" for line in paragraphs.split('\n\n'))
            submission.reply(comment_text)
            logger.info(f"Posted Paragraph Text:\n{comment_text}")
            logger.info(f"Commented first three paragraphs on: {entry.title}")
        else:
            logger.info(f"No paragraphs extracted for {entry.title}; skipping comment")
        add_to_dedup(entry)
        current_posts.append({'title': post_title, 'post_link': submission.shortlink, 'article_url': entry.link})
        time.sleep(30)  # Respect Reddit rate limits
    except Exception as e:
        logger.error(f"Error posting article '{entry.title}': {e}")

logger.info("\n--- Posts Created in This Run ---")
if current_posts:
    for post in current_posts:
        logger.info(f"Title: {post['title']}")
        logger.info(f"Reddit Link: {post['post_link']}")
        logger.info(f"Article URL: {post['article_url']}")
else:
    logger.info("No posts created in this run.")

logger.info("\n--- Historical Posted Records ---")
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                logger.info(line.strip())
else:
    logger.info("No historical posted records found.")

logger.info("Posting process complete.")
