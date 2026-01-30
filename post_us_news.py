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
import difflib
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
    'REDDITPASSWORD'
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
    password=os.environ['REDDITPASSWORD'],
    user_agent='USANewsFlashBot/2.0'
)
subreddit = reddit.subreddit('USANewsFlash')

# Deduplication file
DEDUP_FILE = './posted_usanewsflash_timestamps.txt'

# --- Text Normalization & Helper Functions ---

def normalize_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        # Remove query parameters often used for tracking (utm_source, etc)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))
    except:
        return url

def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s]', '', title) # Remove punctuation
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def load_dedup(filename=DEDUP_FILE):
    """Loads history. Returns a list of (url, title_normalized, timestamp) tuples."""
    history = []
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    parts = line.strip().split('|')
                    if len(parts) >= 3:
                        # Format: timestamp|url|title|hash
                        # We reconstruct loosely to handle potential pipe issues in titles
                        ts = parts[0]
                        url = parts[1]
                        # Join middle parts in case title had pipes, ignore hash at end
                        title_raw = "|".join(parts[2:-1]) 
                        history.append({
                            'url': url,
                            'title_norm': normalize_title(title_raw),
                            'timestamp': ts
                        })
    logger.info(f"Loaded {len(history)} entries from history.")
    return history

# Load history globally once
posted_history = load_dedup()

def is_duplicate_fuzzy(entry, history, threshold=0.85):
    """
    Checks if entry is a duplicate using URL match OR Fuzzy Title Match.
    Returns: (Boolean, Reason)
    """
    current_url = normalize_url(entry.link)
    current_title_norm = normalize_title(entry.title)
    
    for item in history:
        # 1. URL Check
        if current_url == item['url']:
            return True, "Exact URL Match"
        
        # 2. Fuzzy Title Check
        similarity = difflib.SequenceMatcher(None, current_title_norm, item['title_norm']).ratio()
        if similarity > threshold:
            return True, f"Fuzzy Title Match ({similarity:.2f})"
            
    return False, ""

def add_to_dedup(entry):
    norm_link = normalize_url(entry.link)
    clean_title = html.unescape(entry.title).replace("|", "-").strip() # Sanitize pipes for storage
    content_hash = hashlib.md5(clean_title.encode('utf-8')).hexdigest()
    
    # Write to file
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}|{norm_link}|{clean_title}|{content_hash}\n")
    
    # Update memory
    posted_history.append({
        'url': norm_link,
        'title_norm': normalize_title(clean_title),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

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

# --- Content Fetching ---

def fetch_article_content(url, fallback_summary):
    """
    Attempts to fetch the first 3 paragraphs of the article.
    Falls back to RSS summary if scraping fails.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    
    try:
        # Short timeout to prevent hanging the script
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove scripts, styles, navs
            for script in soup(["script", "style", "nav", "header", "footer", "aside"]):
                script.decompose()
            
            paragraphs = soup.find_all('p')
            text_blocks = []
            
            for p in paragraphs:
                text = p.get_text(strip=True)
                # Filter out short "read more" or meta lines
                if len(text) > 60 and "cookies" not in text.lower() and "subscribe" not in text.lower():
                    text_blocks.append(text)
                    if len(text_blocks) >= 3:
                        break
            
            if text_blocks:
                return "\n\n".join(text_blocks)
                
    except Exception as e:
        logger.warning(f"Failed to scrape {url}: {e}")
    
    # Fallback
    logger.info("Using fallback RSS summary.")
    soup = BeautifulSoup(fallback_summary, 'html.parser')
    return soup.get_text(strip=True)

# --- Scoring & Filters ---

US_KEYWORDS = {
    # High Priority (Gov/Geography)
    "washington dc": 3, "congress": 3, "senate": 3, "white house": 3, "capitol hill": 3,
    "california": 3, "texas": 3, "new york": 3, "florida": 3, "illinois": 3, "pennsylvania": 3,
    "ohio": 3, "georgia": 3, "north carolina": 3, "michigan": 3, "arizona": 3, "nevada": 3,
    "fbi": 3, "cia": 3, "pentagon": 3, "supreme court": 3, "president": 3, "biden": 2, "trump": 2, "harris": 2,
    
    # Disasters/Crime (High Impact)
    "shooting": 3, "gun": 2, "murder": 2, "hurricane": 3, "tornado": 3, "wildfire": 3,
    
    # General US
    "united states": 2, "usa": 2, "american": 2, "federal": 2, "constitution": 2,
    "dollar": 1, "economy": 1, "inflation": 1, "wall street": 2, "nfl": 3, "nba": 3,
    
    # Royals Exception (To allow them to pass relevance check)
    "royal family": 3, "king charles": 3, "prince william": 3, "prince harry": 3, "meghan markle": 3, 
    "buckingham palace": 2, "princess of wales": 3
}

NEGATIVE_KEYWORDS = {
    "uk parliament": -5, "nhs": -5, "brexit": -5, "premier league": -4,
    "rishi sunak": -4, "keir starmer": -4, "tory": -4, "labour party": -4,
    "australia": -3, "canada": -3, "trudeau": -3, "toronto": -3, "ontario": -3,
    "india": -2, "china": -2, "russia": -1, "ukraine": -1, # Context dependent, lower penalty
    "euro": -2, "champions league": -3
}

def calculate_relevance(text):
    score = 0
    text_lower = text.lower()
    matched_keywords = []
    
    for keyword, weight in US_KEYWORDS.items():
        if keyword in text_lower:
            score += weight
            if weight > 0:
                matched_keywords.append(keyword)
                
    for keyword, weight in NEGATIVE_KEYWORDS.items():
        if keyword in text_lower:
            score += weight
            
    # Deduplicate matching keywords
    matched_keywords = list(set(matched_keywords))
    return score, matched_keywords

def is_promotional_or_spam(entry):
    title = entry.title.lower()
    spam_words = [
        "giveaway", "win", "coupon", "promo", "deal of the day", "best price",
        "how to", "guide to", "tutorial", "review:", "unboxing"
    ]
    if any(x in title for x in spam_words):
        return True
    return False

def is_opinion_piece(entry, source_name):
    title = entry.title.lower()
    link = entry.link.lower()
    
    indicators = ["opinion", "op-ed", "editorial", "perspective", "analysis", "letters to the editor"]
    
    # Strict check for NYT
    if source_name == "NY Times" and "/opinion/" in link:
        return True
        
    if any(ind in title for ind in indicators):
        return True
        
    return False

# --- Categories ---

CATEGORY_RULES = {
    "Royals": ["royal", "king charles", "queen", "prince", "princess", "monarch", "harry", "meghan"],
    "Politics": ["congress", "senate", "white house", "biden", "trump", "election", "vote", "bill", "law", "democrat", "republican"],
    "Crime & Legal": ["police", "arrest", "shot", "killed", "court", "judge", "trial", "guilty", "suspect", "fbi", "investigation"],
    "Sports": ["nfl", "nba", "mlb", "nhl", "football", "basketball", "baseball", "super bowl", "touchdown", "espn"],
    "Entertainment": ["movie", "film", "star", "celebrity", "actor", "actress", "hollywood", "netflix", "grammy", "oscar", "concert"]
}

def get_category_and_flair(text):
    text = text.lower()
    
    # Priority Check: Royals
    if any(kw in text for kw in CATEGORY_RULES["Royals"]):
        return "Royals"
        
    for cat, keywords in CATEGORY_RULES.items():
        if cat == "Royals": continue
        if any(kw in text for kw in keywords):
            return cat
            
    return "Breaking News"

FLAIR_IDS = {} # Populated at runtime if possible, or matched by text

# --- Posting Logic ---

def post_to_reddit(entry, source_name, score, keywords):
    
    # 1. Clean Title
    clean_title = html.unescape(entry.title).strip()
    
    # 2. Determine Flair
    full_text = f"{clean_title} {getattr(entry, 'summary', '')}"
    category = get_category_and_flair(full_text)
    
    # 3. Get Content (Scrape or Fallback)
    article_content = fetch_article_content(entry.link, getattr(entry, "summary", "No summary available."))
    
    # 4. Prepare Reply Comment
    # Truncate content for comment if super long, but usually 3 paragraphs is fine.
    # We ensure quotes are properly formatted.
    quoted_content = "\n\n".join([f"> {para}" for para in article_content.split("\n\n")])
    
    reply_body = (
        f"Source: {source_name}\n\n"
        f"{quoted_content}\n\n"
        f"US Relevance Score: {score} | Keywords: {', '.join(keywords)}\n\n"
        f"[Read more]({entry.link})"
    )

    # 5. Submit
    try:
        # Check for flair ID match
        flair_id = None
        if subreddit.flair.link_templates:
            for f in subreddit.flair.link_templates:
                if f['text'] == category:
                    flair_id = f['id']
                    break
        
        # Submit Link
        submission = subreddit.submit(
            title=clean_title,
            url=entry.link,
            flair_id=flair_id
        )
        
        # Post Comment
        submission.reply(reply_body)
        
        logger.info(f"SUCCESS: Posted '{clean_title}' [{category}]")
        add_to_dedup(entry)
        return True
        
    except Exception as e:
        logger.error(f"FAILED to post '{clean_title}': {e}")
        return False

# --- Main Execution ---

def main():
    feed_sources = {
        "CNN": "https://rss.cnn.com/rss/cnn_topstories.rss",
        "NBC News": "https://feeds.nbcnews.com/nbcnews/public/news",
        "ABC News": "https://abcnews.go.com/abcnews/topstories",
        "NY Times": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "BBC US": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"
    }

    found_articles = []
    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(hours=4) # Extended slightly to catch slow feeds
    
    # 1. Gather Articles
    for name, url in feed_sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                
                # Time Check
                pub_date = get_entry_published_datetime(entry)
                if not pub_date or pub_date < cutoff_time:
                    continue
                
                # Dedup Check (Fuzzy)
                is_dup, reason = is_duplicate_fuzzy(entry, posted_history)
                if is_dup:
                    logger.info(f"Skipping Duplicate ({reason}): {entry.title[:30]}...")
                    continue
                
                # Filter Checks
                if is_promotional_or_spam(entry):
                    continue
                if is_opinion_piece(entry, name):
                    continue
                
                # Relevance Check
                combined_text = f"{entry.title} {getattr(entry, 'summary', '')}"
                score, keywords = calculate_relevance(combined_text)
                
                # Thresholds
                # BBC often mixes Canada news, so we enforce a stricter score unless "US" or states are explicitly mentioned
                threshold = 3
                if score >= threshold:
                    found_articles.append({
                        'entry': entry,
                        'source': name,
                        'score': score,
                        'keywords': keywords,
                        'time': pub_date
                    })
                else:
                    logger.debug(f"Low Relevance ({score}): {entry.title}")

        except Exception as e:
            logger.error(f"Error reading feed {name}: {e}")

    # 2. Sort and Select
    # Sort by Score (Desc), then Recency
    found_articles.sort(key=lambda x: (x['score'], x['time']), reverse=True)
    
    # Select top 5 to prevent flooding
    to_post = found_articles[:5]
    
    if not to_post:
        logger.info("No eligible articles found this run.")
        return

    # 3. Post
    for item in to_post:
        post_to_reddit(item['entry'], item['source'], item['score'], item['keywords'])
        time.sleep(10) # Safety delay between posts

if __name__ == "__main__":
    main()
