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
    'REDDITPASSWORD'  # Reddit password environment variable
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
DEDUP_FILE = './posted_timestamps.txt'  # File stored in repository root

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
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return f"(Could not extract article text: {e})"

# --- Filter Keywords ---
PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement"
]

# Expanded UK-relevant keywords with weights
UK_KEYWORDS = {
    # High weight (3) - Strongly indicative of UK context
    "london": 3, "parliament": 3, "westminster": 3, "downing street": 3, "buckingham palace": 3,
    "nhs": 3, "bank of england": 3, "ofgem": 3, "bbc": 3, "itv": 3, "sky news": 3,
    "manchester": 3, "birmingham": 3, "glasgow": 3, "edinburgh": 3, "cardiff": 3, "belfast": 3,
    "liverpool": 3, "leeds": 3, "bristol": 3, "newcastle": 3, "sheffield": 3, "nottingham": 3,
    "leominster": 3, "herefordshire": 3, "shropshire": 3, "worcestershire": 3, "devon": 3, "cornwall": 3,
    "norfolk": 3, "suffolk": 3, "kent": 3, "sussex": 3, "essex": 3, "yorkshire": 3, "cumbria": 3,
    "premier league": 3, "wimbledon": 3, "glastonbury": 3, "the ashes": 3, "royal ascot": 3,
    "house of commons": 3, "house of lords": 3, "met police": 3, "scotland yard": 3,
    "national trust": 3, "met office": 3, "british museum": 3, "tate modern": 3,
    "level crossing": 3, "west midlands railway": 3, "network rail": 3,
    # Medium weight (2) - Generally UK-related but less specific
    "uk": 2, "britain": 2, "united kingdom": 2, "england": 2, "scotland": 2, "wales": 2, "northern ireland": 2,
    "british": 2, "labour": 2, "conservative": 2, "lib dem": 2, "snp": 2, "green party": 2,
    "king charles": 2, "queen camilla": 2, "prince william": 2, "princess kate": 2,
    "keir starmer": 2, "rachel reeves": 2, "kemi badenoch": 2, "ed davey": 2, "john swinney": 2,
    "angela rayner": 2, "nigel farage": 2, "carla denyer": 2, "adrian ramsay": 2,
    "brexit": 2, "pound sterling": 2, "great british": 2, "oxford": 2, "cambridge": 2,
    "village": 2, "county": 2, "borough": 2, "railway": 2,
    # Low weight (1) - Broad terms that may appear in UK context
    "government": 1, "economy": 1, "policy": 1, "election": 1, "inflation": 1, "cost of living": 1,
    "prime minister": 1, "chancellor": 1, "home secretary": 1, "a-levels": 1, "gcse": 1,
    "council tax": 1, "energy price cap": 1, "high street": 1, "pub": 1, "motorway": 1
}

# Expanded negative keywords with weights
NEGATIVE_KEYWORDS = {
    # High negative weight (-2) - Strongly indicative of non-UK context
    "washington dc": -2, "congress": -2, "senate": -2, "white house": -2, "capitol hill": -2,
    "california": -2, "texas": -2, "new york": -2, "los angeles": -2, "chicago": -2,
    "florida": -2, "boston": -2, "miami": -2, "san francisco": -2, "seattle": -2,
    "fbi": -2, "cia": -2, "pentagon": -2, "supreme court": -2, "biden": -2, "trump": -2,
    "super bowl": -2, "nfl": -2, "nba": -2, "wall street": -2,
    # Medium negative weight (-1) - General international terms
    "france": -1, "germany": -1, "china": -1, "russia": -1, "india": -1,
    "australia": -1, "canada": -1, "japan": -1, "brazil": -1, "south africa": -1,
    "paris": -1, "berlin": -1, "tokyo": -1, "sydney": -1, "toronto": -1,
    "nato": -1, "united nations": -1, "european union": -1, "olympics": -1, "world cup": -1
}

def calculate_uk_relevance_score(text):
    """Calculate a relevance score with bonus for UK-like place names."""
    score = 0
    text_lower = text.lower()
    for keyword, weight in UK_KEYWORDS.items():
        if keyword in text_lower:
            score += weight
    for keyword, weight in NEGATIVE_KEYWORDS.items():
        if keyword in text_lower:
            score += weight  # weight is negative
    # Bonus for UK-like place names (e.g., ending in 'shire', 'ford', 'ton')
    if re.search(r'\b\w+(shire|ford|ton|ham|bridge)\b', text_lower):
        score += 2
    return score

def is_promotional(entry):
    """Check if an article is promotional, allowing 'offer' in government/policy contexts."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    # Allow 'offer' if paired with government/policy-related terms
    if "offer" in combined:
        if any(kw in combined for kw in ["government", "nhs", "pay", "policy", "public sector"]):
            return False
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)

def is_uk_relevant(entry, threshold=2):
    """Check if an article is UK-relevant based on the calculated score and print the score."""
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    score = calculate_uk_relevance_score(combined)
    # Print the relevance score for each article
    print(f"Article: {html.unescape(entry.title)} | Relevance Score: {score}")
    if score < threshold:
        logger.info(f"Filtered out article with score {score}: {html.unescape(entry.title)}")
    return score >= threshold

# --- Category Keywords ---
CATEGORY_KEYWORDS = {
    "Breaking News": ["breaking", "live", "update", "developing", "just in", "alert"],
    "Politics": ["politics", "parliament", "government", "election", "policy", "minister", "mp", "prime minister"],
    "Crime & Legal": ["crime", "police", "court", "legal", "arrest", "trial", "investigation", "prosecution"],
    "Sport": ["sport", "football", "cricket", "tennis", "olympics", "match", "game", "tournament"],
    "Royals": ["royal", "monarchy", "king", "queen", "prince", "princess", "palace", "crown"]
}

def get_category(entry):
    """Determine the category of an article based on keywords with whole word matching."""
    text = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    specific_categories = ["Politics", "Crime & Legal", "Sport", "Royals"]
    for cat in specific_categories:
        for keyword in CATEGORY_KEYWORDS[cat]:
            if re.search(r'\b' + re.escape(keyword) + r'\b', text):
                return cat
    # Default to "Breaking News" for relevant articles not matching specific categories
    return "Breaking News"

FLAIR_MAPPING = {
    "Breaking News": "Breaking News",
    "Politics": "Politics",
    "Crime & Legal": "Crime & Legal",
    "Sport": "Sport",
    "Royals": "Royals",
    None: "No Flair"
}

def post_to_reddit(entry, category, retries=3, base_delay=40):
    """Post an article to Reddit with flair and a comment."""
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
    """Main function to fetch RSS feeds, filter articles, and post at least 5 unique news stories."""
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "ITV": "https://www.itv.com/news/rss",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
        "Times": "https://www.thetimes.co.uk/rss"
    }
    # Collect articles by source
    articles_by_source = {source: [] for source in feed_sources}
    now = datetime.now(timezone.utc)
    three_hours_ago = now - timedelta(hours=3)

    # Fetch and filter articles from each source
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
                if not is_uk_relevant(entry):
                    logger.info(f"Skipped non-UK article: {html.unescape(entry.title)}")
                    continue
                articles_by_source[name].append(entry)
        except Exception as e:
            logger.error(f"Error loading feed {name}: {e}")

    # Group articles by story (based on deduplication criteria)
    story_groups = []
    used_hashes = set()
    for source in articles_by_source:
        for entry in articles_by_source[source]:
            is_dup, reason = is_duplicate(entry)
            content_hash = get_content_hash(entry)
            if is_dup and content_hash in used_hashes:
                continue  # Skip articles already grouped
            # Find all articles with the same story (same title or hash)
            group = [(source, entry)]
            for other_source in articles_by_source:
                if other_source == source:
                    continue
                for other_entry in articles_by_source[other_source]:
                    if other_entry == entry:
                        continue
                    other_hash = get_content_hash(other_entry)
                    other_title = normalize_title(get_post_title(other_entry))
                    entry_title = normalize_title(get_post_title(entry))
                    if other_hash == content_hash or other_title == entry_title:
                        group.append((other_source, other_entry))
            if group:
                story_groups.append(group)
                used_hashes.add(content_hash)

    # Randomize story groups and select one article per story
    random.shuffle(story_groups)
    posts_made = 0
    sources_used = set()
    selected_articles = []

    # First pass: Try to get one article from each source
    for group in story_groups:
        if posts_made >= 5:
            break
        # Randomly select one article from the group
        random.shuffle(group)
        for source, entry in group:
            if source not in sources_used:
                is_dup, reason = is_duplicate(entry)
                if not is_dup:
                    selected_articles.append((source, entry))
                    sources_used.add(source)
                    posts_made += 1
                    break

    # Second pass: Fill remaining slots with any valid article
    if posts_made < 5:
        for group in story_groups:
            if posts_made >= 5:
                break
            random.shuffle(group)
            for source, entry in group:
                is_dup, reason = is_duplicate(entry)
                if not is_dup and (source, entry) not in selected_articles:
                    selected_articles.append((source, entry))
                    posts_made += 1
                    break

    # Post selected articles
    for source, entry in selected_articles:
        category = get_category(entry)
        success = post_to_reddit(entry, category)
        if success:
            logger.info(f"Posted from {source}: {html.unescape(entry.title)}")
            time.sleep(40)
        else:
            posts_made -= 1  # Decrement if post fails

    if posts_made < 5:
        logger.warning(f"Could only post {posts_made} articles; not enough unique, valid news stories found")
    else:
        logger.info(f"Successfully posted {posts_made} articles")

if __name__ == "__main__":
    main()
