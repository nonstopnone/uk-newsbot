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
import json

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- Environment Variable Check ---
required_env_vars = [
    'REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME', 'REDDITPASSWORD'
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

try:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent='BreakingUKNewsBot/1.3' # Version bump
    )
    subreddit = reddit.subreddit('BreakingUKNews')
    logger.info(f"Successfully connected to Reddit as u/{reddit.user.me()}")
except Exception as e:
    logger.error(f"Failed to initialize Reddit API: {e}")
    sys.exit(1)

# --- Deduplication (Using robust JSON format) ---
DEDUP_FILE = './posted_articles.json'

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'ICID']:
        query.pop(key, None)
    query_string = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', query_string, ''))

def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    return re.sub(r'\s+', ' ', title).strip().lower()

def get_post_title(entry):
    return html.unescape(entry.title).strip()

def get_content_hash(entry):
    content = html.unescape(entry.title + " " + getattr(entry, "summary", "")[:300])
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def load_dedup_data(filename=DEDUP_FILE):
    urls, titles, hashes = set(), set(), set()
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    if not os.path.exists(filename): return urls, titles, hashes
    with open(filename, 'r', encoding='utf-8') as f:
        try: data = json.load(f)
        except json.JSONDecodeError: return urls, titles, hashes
    cleaned_data = []
    for item in data:
        try:
            timestamp = dateparser.parse(item['timestamp'])
            if timestamp > seven_days_ago:
                cleaned_data.append(item)
                urls.add(item['url'])
                titles.add(item['normalized_title'])
                hashes.add(item['hash'])
        except (KeyError, dateparser.ParserError): continue
    with open(filename, 'w', encoding='utf-8') as f: json.dump(cleaned_data, f, indent=4)
    logger.info(f"Loaded {len(urls)} unique entries from deduplication file")
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup_data()

def is_duplicate(entry):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(get_post_title(entry))
    content_hash = get_content_hash(entry)
    if norm_link in posted_urls: return True, "Duplicate URL"
    for pt in posted_titles:
        if difflib.SequenceMatcher(None, pt, norm_title).ratio() > 0.92:
            return True, "Duplicate Title (Fuzzy Match)"
    if content_hash in posted_hashes: return True, "Duplicate Content Hash"
    return False, ""

def add_to_dedup(entry):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(get_post_title(entry))
    content_hash = get_content_hash(entry)
    new_entry_data = {
        'timestamp': datetime.now(timezone.utc).isoformat(), 'url': norm_link,
        'normalized_title': norm_title, 'hash': content_hash, 'original_title': get_post_title(entry)
    }
    try:
        with open(DEDUP_FILE, 'r+', encoding='utf-8') as f:
            data = json.load(f); data.append(new_entry_data); f.seek(0); json.dump(data, f, indent=4)
    except (FileNotFoundError, json.JSONDecodeError):
        with open(DEDUP_FILE, 'w', encoding='utf-8') as f: json.dump([new_entry_data], f, indent=4)
    posted_urls.add(norm_link); posted_titles.add(norm_title); posted_hashes.add(content_hash)
    logger.info(f"Added to deduplication: {norm_title}")

def get_entry_published_datetime(entry):
    for field in ['published', 'updated', 'created', 'date']:
        if hasattr(entry, field):
            try:
                dt = dateparser.parse(getattr(entry, field))
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (dateparser.ParserError, TypeError): continue
    return None

def extract_article_text(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        article_body = (soup.find('article') or soup.find('div', class_=re.compile(r'article|content|post|body', re.I)) or soup.find('main'))
        target_element = article_body if article_body else soup
        paragraphs = [p.get_text(strip=True) for p in target_element.find_all('p') if len(p.get_text(strip=True)) > 100]
        if not paragraphs: return f"(Article summary could not be automatically extracted.)"
        text = '\n\n'.join(paragraphs[:3])
        return text[:1500] + '...' if len(text) > 1500 else text
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url} for text extraction: {e}")
        return f"(Could not extract article text due to a network error.)"

# --- Filtering & Categorization ---
PROMOTIONAL_KEYWORDS = [
    "advert", "advertisement", "advertorial", "black friday", "competition", "coupon",
    "deal", "discount", "exclusive offer", "free", "giveaway", "limited time", "offer",
    "partnered", "prize", "promo code", "sale", "sponsor", "sponsored", "subscribe",
    "voucher", "win"
]
OPINION_KEYWORDS = [
    "analysis", "column", "comment", "editorial", "i believe", "letter to the editor",
    "my take", "opinion", "our view", "perspective", "viewpoint"
]

# USER REQUEST: Dedicated list for climate change exclusion
CLIMATE_CHANGE_KEYWORDS = [
    "climate change", "global warming", "net zero", "green energy", "cop26", "cop27", "cop28",
    "decarbonisation", "carbon emissions", "greenhouse gas", "renewable energy",
    "sustainability", "extinction rebellion", "just stop oil", "climate crisis", "eco-warrior"
]

# USER REQUEST: Keywords to identify protests for relevance boost
PROTEST_KEYWORDS = [
    "protest", "pro-palestine", "protestors", "protesters", "march", "rally",
    "demonstration", "activists", "chanting", "picket", "sit-in"
]
MAJOR_UK_CITIES = [
    "london", "manchester", "birmingham", "glasgow", "edinburgh",
    "cardiff", "belfast", "liverpool", "bristol", "leeds"
]

UK_KEYWORDS = {
    # Tier 3 (Highly Specific UK Terms)
    "nhs": 3, "parliament": 3, "downing street": 3, "westminster": 3, "house of commons": 3,
    "scotland yard": 3, "bank of england": 3, "hmrc": 3, "ofgem": 3, "ofsted": 3, "dwp": 3,
    "bbc": 3, "tory": 3, "labour party": 3, "lib dem": 3, "reform uk": 3, "snp": 3, "senedd": 3,
    "london": 3, "manchester": 3, "birmingham": 3, "glasgow": 3, "edinburgh": 3, "cardiff": 3, "belfast": 3,
    "liverpool": 3, "leeds": 3, "sheffield": 3, "bristol": 3, "newcastle": 3, "nottingham": 3, "leicester": 3,
    "heathrow": 3, "gatwick": 3, "m25": 3, "hs2": 3, "national trust": 3, "royal mail": 3,
    "premier league": 3, "wimbledon": 3, "the ashes": 3, "glastonbury": 3,
    # Tier 2 (Strong UK Indicators)
    "uk": 2, "britain": 2, "united kingdom": 2, "england": 2, "scotland": 2, "wales": 2, "northern ireland": 2,
    "british": 2, "sterling": 2, "brexit": 2, "gbp": 2,
    "king charles": 2, "queen camilla": 2, "prince william": 2, "princess catherine": 2, "buckingham palace": 2,
    "keir starmer": 2, "rishi sunak": 2, "nigel farage": 2, "ed davey": 2, "liz truss": 2, "boris johnson": 2,
    "labour": 2, "conservative": 2, "chancellor": 2, "home office": 2, "defra": 2,
    "a-levels": 2, "gcse": 2, "met office": 2, "county": 2, "constituency": 2,
    "yorkshire": 2, "cornwall": 2, "devon": 2, "kent": 2, "essex": 2, "cumbria": 2,
    # Tier 1 (Contextual UK Terms)
    "government": 1, "election": 1, "prime minister": 1, "mp": 1, "peer": 1,
    "cost of living": 1, "inflation": 1, "recession": 1, "high street": 1,
    "council tax": 1, "motorway": 1, "trainline": 1, "pub": 1, "village": 1
}
NEGATIVE_KEYWORDS = {
    # Tier -3 (Strongly US-Specific)
    "congress": -3, "white house": -3, "senate": -3, "capitol hill": -3, "pentagon": -3,
    "biden": -3, "trump": -3, "harris": -3, "desantis": -3, "potus": -3, "scotus": -3,
    "fbi": -3, "cia": -3, "irs": -3, "nfl": -3, "nba": -3, "mlb": -3, "super bowl": -3, "wall street": -3,
    "washington dc": -3, "california": -3, "texas": -3, "florida": -3, "new york": -3,
    "los angeles": -3, "chicago": -3, "miami": -3, "san francisco": -3,
    # Tier -2 (Strong International / Non-UK)
    "beijing": -2, "moscow": -2, "tokyo": -2, "delhi": -2, "ottawa": -2, "canberra": -2,
    "china": -2, "russia": -2, "india": -2, "japan": -2, "australia": -2, "canada": -2,
    "france": -2, "germany": -2, "spain": -2, "italy": -2, "brazil": -2, "south africa": -2,
    "nato": -2, "european union": -2, "united nations": -2,
    # Tier -1 (General International Context)
    "world cup": -1, "olympics": -1, "euro": -1, "dollar": -1, "federal": -1
}

def calculate_uk_relevance_score(text):
    score = 0
    text_lower = text.lower()
    for keyword, weight in {**UK_KEYWORDS, **NEGATIVE_KEYWORDS}.items():
        if re.search(r'\b' + re.escape(keyword) + r'\b', text_lower):
            score += weight
    if re.search(r'\b\w+(shire|ford|ton|ham|bridge|cester|borough|bury)\b', text_lower):
        score += 2
    
    # USER REQUEST: Boost score for protests happening in the UK about foreign issues
    is_protest = any(re.search(r'\b' + kw + r'\b', text_lower) for kw in PROTEST_KEYWORDS)
    is_in_uk_city = any(re.search(r'\b' + city + r'\b', text_lower) for city in MAJOR_UK_CITIES)
    if is_protest and is_in_uk_city:
        score += 4 # Significant boost to ensure inclusion
        logger.info(f"Applying protest relevance boost.")

    return score

def is_promotional(entry):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    if "offer" in combined and any(kw in combined for kw in ["government", "nhs", "pay", "union", "sector"]):
        return False
    return any(re.search(r'\b' + kw + r'\b', combined) for kw in PROMOTIONAL_KEYWORDS)

def is_opinion(entry):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(re.search(r'\b' + kw + r'\b', combined) for kw in OPINION_KEYWORDS)

# USER REQUEST: New function to filter out climate change articles
def is_climate_related(entry):
    """Check if an article is primarily about climate change."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(re.search(r'\b' + kw + r'\b', combined) for kw in CLIMATE_CHANGE_KEYWORDS)

def is_uk_relevant(entry, threshold=3):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", ""))
    score = calculate_uk_relevance_score(combined)
    if score < threshold:
        logger.info(f"Filtered out low relevance (Score: {score}): {entry.title}")
        return False
    logger.info(f"Relevant article found (Score: {score}): {entry.title}")
    return True

CATEGORY_KEYWORDS = {
    "Politics": ["politics", "parliament", "government", "election", "policy", "minister", "mp", "prime minister", "brexit", "tory", "labour", "lib dem", "reform uk", "snp", "westminster", "downing street", "senedd"],
    "Crime & Legal": ["crime", "police", "court", "legal", "arrest", "trial", "investigation", "prosecution", "jailed", "sentenced", "convicted", "manslaughter", "murder", "theft"],
    "Sport": ["sport", "football", "cricket", "tennis", "olympics", "match", "game", "tournament", "rugby", "f1", "premier league", "wimbledon", "ashes"],
    "Royals": ["royal", "monarchy", "king", "queen", "prince", "princess", "palace", "duke", "duchess", "windsor", "buckingham"],
    "Economy": ["economy", "budget", "inflation", "gdp", "recession", "bank of england", "chancellor", "cost of living", "interest rates", "ftse", "business", "markets"],
    "Health": ["health", "nhs", "hospital", "doctor", "pandemic", "vaccine", "covid", "mental health", "care", "surgery"],
    "Education": ["education", "school", "university", "a-levels", "gcse", "ofsted", "student", "teacher", "pupil"],
    "UK News": [] # Fallback
}
def get_category(entry):
    text = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    if any(re.search(r'\b' + kw + r'\b', text) for kw in ["breaking", "live", "update", "developing"]): return "Breaking News"
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(re.search(r'\b' + re.escape(kw) + r'\b', text) for kw in keywords): return cat
    return "UK News"

def post_to_reddit(entry, category, retries=3, base_delay=60):
    flair_id = None
    try:
        flairs = subreddit.flair.link_templates
        for flair in flairs:
            if flair['text'].lower() == category.lower():
                flair_id = flair['id']; break
        if not flair_id: logger.warning(f"Could not find flair for category: '{category}'")
    except Exception as e:
        logger.error(f"Failed to fetch subreddit flairs: {e}")
    for attempt in range(retries):
        try:
            post_title = get_post_title(entry)
            submission = subreddit.submit(title=post_title, url=entry.link, flair_id=flair_id)
            logger.info(f"✅ Posted to Reddit: {submission.shortlink}")
            article_summary = extract_article_text(entry.link)
            reply_text = "\n\n".join([f"> {html.unescape(line)}" for line in article_summary.split('\n\n')])
            submission.reply(f"{reply_text}\n\n---\n\n[Read the full story here]({entry.link})")
            add_to_dedup(entry)
            return True
        except praw.exceptions.RedditAPIException as e:
            if "RATELIMIT" in str(e):
                delay = base_delay * (2 ** attempt) + random.uniform(0, 15)
                logger.warning(f"Rate limit hit. Retrying in {int(delay)}s (Attempt {attempt + 1}/{retries})")
                time.sleep(delay)
            else:
                logger.error(f"PRAW API Error for '{entry.title}': {e}"); return False
        except Exception as e:
            logger.error(f"An unexpected error occurred while posting '{entry.title}': {e}"); return False
    logger.error(f"Failed to post '{entry.title}' after {retries} attempts."); return False

def main():
    MIN_POSTS_PER_RUN = 5
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky News UK": "https://feeds.skynews.com/feeds/rss/uk.xml",
        "Reuters UK": "https://www.reuters.com/tools/rss/reutersEdge?edition=uk",
        "Telegraph": "https://www.telegraph.co.uk/news/rss.xml",
    }
    all_articles = []
    now = datetime.now(timezone.utc)
    time_window = now - timedelta(hours=4)
    feed_items = list(feed_sources.items())
    random.shuffle(feed_items)
    for name, url in feed_items:
        logger.info(f"--- Fetching feed: {name} ---")
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published_dt = get_entry_published_datetime(entry)
                if not published_dt or published_dt < time_window or published_dt > now + timedelta(minutes=10): continue
                
                # Apply new filters first
                if is_climate_related(entry):
                    logger.info(f"Skipped climate-related article: {entry.title}")
                    continue
                if is_promotional(entry) or is_opinion(entry): continue
                is_dup, reason = is_duplicate(entry)
                if is_dup: continue
                
                # Check relevance last
                if is_uk_relevant(entry):
                    all_articles.append((name, entry))
        except Exception as e:
            logger.error(f"Error processing feed {name}: {e}")

    all_articles.sort(key=lambda x: get_entry_published_datetime(x[1]), reverse=True)
    logger.info(f"Found {len(all_articles)} new, relevant articles to consider for posting.")
    posts_made = 0
    for source, entry in all_articles:
        if posts_made >= MIN_POSTS_PER_RUN: break
        category = get_category(entry)
        if post_to_reddit(entry, category):
            posts_made += 1
            if posts_made < MIN_POSTS_PER_RUN: time.sleep(random.uniform(45, 75))
        else:
            logger.error(f"Stopping run due to posting failure for: {entry.title}"); break
    logger.info(f"Run complete. Successfully posted {posts_made} articles.")

if __name__ == "__main__":
    main()
