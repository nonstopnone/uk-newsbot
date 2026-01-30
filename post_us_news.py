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

# --- Configuration ---
# File to store history of posted articles
DEDUP_FILE = 'posted_usanewsflash_timestamps.txt'

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Environment variable check
required_env_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME', 'REDDITPASSWORD']
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

# --- Deduplication Logic ---

def normalize_title(title):
    """Normalize title for fuzzy comparison (lowercase, remove punctuation)."""
    title = html.unescape(title)
    title = re.sub(r'[^\w\s]', '', title)
    return title.strip().lower()

def normalize_url(url):
    """Strip query parameters to avoid duplicate URL mismatches."""
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))
    except:
        return url

def load_history():
    """Loads posted history from file."""
    history = []
    if not os.path.exists(DEDUP_FILE):
        # Create file if it doesn't exist
        open(DEDUP_FILE, 'w').close()
        return history

    try:
        with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    parts = line.strip().split('|')
                    if len(parts) >= 2:
                        # File format: Timestamp|URL|Title|Hash
                        # We only strictly need URL and Title for checks
                        url = parts[1]
                        title = "|".join(parts[2:-1]) if len(parts) > 3 else parts[2]
                        history.append({
                            'url': url,
                            'title_norm': normalize_title(title)
                        })
    except Exception as e:
        logger.error(f"Error loading deduplication file: {e}")
    return history

def is_duplicate(entry, history):
    """
    Returns (True, Reason) if the entry exists in history.
    Checks: 1. Exact URL 2. Fuzzy Title Match (>85%)
    """
    curr_url = normalize_url(entry.link)
    curr_title_norm = normalize_title(entry.title)

    for item in history:
        if curr_url == item['url']:
            return True, "Same URL"
        
        # Fuzzy match ratio (0.0 to 1.0)
        similarity = difflib.SequenceMatcher(None, curr_title_norm, item['title_norm']).ratio()
        if similarity > 0.85:
            return True, f"Similar Title ({int(similarity*100)}%)"
            
    return False, ""

def save_to_history(entry):
    """Appends the new post to the local file."""
    norm_url = normalize_url(entry.link)
    clean_title = html.unescape(entry.title).replace('|', '-') # Sanitize pipe for storage
    content_hash = hashlib.md5(clean_title.encode()).hexdigest()
    ts = datetime.now(timezone.utc).isoformat()
    
    try:
        with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{ts}|{norm_url}|{clean_title}|{content_hash}\n")
    except Exception as e:
        logger.error(f"Failed to save to history file: {e}")

# --- Content Processing ---

def get_article_content(url, fallback_summary):
    """
    Scrapes the URL for the first 3 paragraphs. 
    Falls back to RSS summary if scraping fails.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove junk elements
            for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'form', 'iframe']):
                tag.decompose()
            
            # Find paragraphs
            paras = soup.find_all('p')
            valid_paras = []
            for p in paras:
                text = p.get_text(strip=True)
                # Basic filter for short/junk paragraphs
                if len(text) > 60 and "cookies" not in text.lower() and "subscribe" not in text.lower():
                    valid_paras.append(text)
                    if len(valid_paras) >= 3:
                        break
            
            if valid_paras:
                return valid_paras
    except Exception as e:
        logger.warning(f"Scraping failed for {url}: {e}")
        
    # Fallback to summary
    soup = BeautifulSoup(fallback_summary, 'html.parser')
    text = soup.get_text(strip=True)
    # Split by periods to fake paragraphs if needed, or just return as one block
    return [text]

# --- Keyword & Relevance Logic ---

US_KEYWORDS = {
    "washington dc": 3, "congress": 3, "senate": 3, "white house": 3, "capitol hill": 3,
    "california": 3, "texas": 3, "new york": 3, "florida": 3, "pennsylvania": 3,
    "georgia": 3, "north carolina": 3, "michigan": 3, "arizona": 3, "nevada": 3,
    "fbi": 3, "cia": 3, "pentagon": 3, "supreme court": 3, "president": 3, 
    "biden": 2, "trump": 2, "harris": 2, "obama": 2, "vance": 2, "walz": 2,
    "shooting": 3, "gun violence": 3, "hurricane": 3, "tornado": 3, "wildfire": 3,
    "united states": 2, "usa": 2, "american": 2, "federal": 2, "dollar": 1,
    "nfl": 3, "nba": 3, "mlb": 3, "royal family": 3, "king charles": 3, "prince william": 3
}

NEGATIVE_KEYWORDS = {
    "uk parliament": -5, "nhs": -5, "brexit": -5, "rishi sunak": -5, "keir starmer": -5,
    "australia": -3, "canada": -3, "trudeau": -3, "toronto": -3, "india": -2, "china": -2
}

def calculate_relevance(text):
    score = 0
    text_lower = text.lower()
    found_keywords = []
    
    for kw, weight in US_KEYWORDS.items():
        if kw in text_lower:
            score += weight
            if weight > 0: found_keywords.append(kw)
            
    for kw, weight in NEGATIVE_KEYWORDS.items():
        if kw in text_lower:
            score += weight
            
    return score, list(set(found_keywords))

def get_flair(text):
    text = text.lower()
    if any(k in text for k in ["royal", "king", "queen", "prince"]): return "Royals"
    if any(k in text for k in ["politics", "congress", "senate", "election", "biden", "trump"]): return "Politics"
    if any(k in text for k in ["crime", "police", "arrest", "court", "judge", "shooting"]): return "Crime & Legal"
    if any(k in text for k in ["sport", "nfl", "nba", "game", "match"]): return "Sports"
    if any(k in text for k in ["movie", "star", "film", "hollywood", "celebrity"]): return "Entertainment"
    return "Breaking News"

# --- Main Logic ---

def main():
    feed_sources = {
        "CNN": "https://rss.cnn.com/rss/cnn_topstories.rss",
        "NBC News": "https://feeds.nbcnews.com/nbcnews/public/news",
        "ABC News": "https://abcnews.go.com/abcnews/topstories",
        "NY Times": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "BBC US": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"
    }

    # Load deduplication history
    history = load_history()
    
    # Collect eligible articles
    candidates = []
    now = datetime.now(timezone.utc)
    
    for source_name, url in feed_sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                
                # 1. Time Check (Last 4 hours)
                pub_date = None
                for field in ['published', 'updated', 'created']:
                    if hasattr(entry, field):
                        try:
                            pub_date = dateparser.parse(getattr(entry, field))
                            if not pub_date.tzinfo: pub_date = pub_date.replace(tzinfo=timezone.utc)
                            pub_date = pub_date.astimezone(timezone.utc)
                            break
                        except: pass
                
                if not pub_date or (now - pub_date) > timedelta(hours=4):
                    continue

                # 2. Deduplication (Fuzzy & Exact)
                is_dup, reason = is_duplicate(entry, history)
                if is_dup:
                    logger.info(f"Skipping Duplicate ({reason}): {entry.title[:30]}")
                    continue

                # 3. Spam/Opinion Filter
                title_lower = entry.title.lower()
                if any(x in title_lower for x in ["giveaway", "deal", "coupon", "best price"]): continue
                if "opinion" in title_lower or "op-ed" in title_lower: continue

                # 4. Relevance Score
                full_text = f"{entry.title} {getattr(entry, 'summary', '')}"
                score, keywords = calculate_relevance(full_text)
                
                # Thresholds: BBC mixed feed needs stricter check unless score is high
                threshold = 3
                if score >= threshold:
                    candidates.append({
                        'entry': entry,
                        'source': source_name,
                        'score': score,
                        'keywords': keywords,
                        'flair': get_flair(full_text)
                    })

        except Exception as e:
            logger.error(f"Error parsing {source_name}: {e}")

    # Sort by Score (Highest first) -> then by Date
    candidates.sort(key=lambda x: x['score'], reverse=True)
    
    # Pick top 5
    to_post = candidates[:5]
    if not to_post:
        logger.info("No relevant articles found.")
        return

    # Post to Reddit
    for item in to_post:
        entry = item['entry']
        try:
            # Prepare Title (Clean)
            post_title = html.unescape(entry.title).strip()
            
            # Prepare Content (Scraped 3 Paragraphs)
            paras = get_article_content(entry.link, getattr(entry, 'summary', ''))
            quote_block = "\n\n".join([f"> {p}" for p in paras])
            
            # Construct Comment
            comment_body = (
                f"Source: {item['source']}\n\n"
                f"{quote_block}\n\n"
                f"US Relevance Score: {item['score']} | Keywords: {', '.join(item['keywords'])}\n\n"
                f"[Read more]({entry.link})"
            )

            # Get Flair ID
            flair_id = None
            if subreddit.flair.link_templates:
                for f in subreddit.flair.link_templates:
                    if f['text'] == item['flair']:
                        flair_id = f['id']
                        break

            # Submit
            submission = subreddit.submit(
                title=post_title,
                url=entry.link,
                flair_id=flair_id
            )
            submission.reply(comment_body)
            
            logger.info(f"Posted: {post_title}")
            
            # Save to history immediately
            save_to_history(entry)
            
            # Update local history list to prevent dupes in same run
            history.append({
                'url': normalize_url(entry.link),
                'title_norm': normalize_title(entry.title)
            })
            
            time.sleep(10) # Rate limit safety

        except Exception as e:
            logger.error(f"Failed to post {entry.title}: {e}")

if __name__ == "__main__":
    main()
