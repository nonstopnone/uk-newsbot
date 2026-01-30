# ===== Section: Imports & Setup =====
import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import re
import hashlib
import html
import random
import difflib
import json
import argparse
from dateutil import parser as dateparser
from collections import defaultdict

# ===== Section: Constants & Configuration =====

# 1. Configuration
TARGET_POSTS = 5            # How many to post per run
MAX_PER_SOURCE = 3          # Max articles from one source per run
TIME_WINDOW_HOURS = 4       # How far back to look
DEDUP_FILE = 'posted_usanewsflash_timestamps.txt'
METRICS_FILE = 'metrics.json'
IN_RUN_FUZZY_THRESHOLD = 0.55  # Strict check for duplicates in the same run
HISTORY_RETENTION_DAYS = 7     # Keep dedup history for 7 days

# 2. Source Definitions (Strictly US Divisions of UK/Intl Media)
FEED_SOURCES = {
    "BBC News US": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "Telegraph US": "https://www.telegraph.co.uk/us/rss.xml",
    "Sky News US": "https://feeds.skynews.com/feeds/rss/us.xml"
}

# 3. Colors for Logging
class Col:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

# ===== Section: Extensive Keyword Lists =====

# Compiled Regex for efficiency
def compile_list(word_list):
    # Matches whole words only, case insensitive
    return [re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE) for w in word_list]

# A. Banned Phrases (Spam, fluff, shopping, opinions)
BANNED_PHRASES = [
    # Shopping / Commercial
    "deal of the day", "best price", "coupon", "promo code", "discount", "sale", "amazon prime",
    "gift guide", "review:", "best smartphone", "where to buy", "shopping", "cyber monday",
    "black friday", "giveaway", "win a", "subscription",
    # Clickbait / Fluff
    "you won't believe", "shocking", "viral", "hacks", "mind-blowing", "insane", "watch:",
    "here's why", "what we know", "everything you need to know", "five things", "10 things",
    "ways to", "how to", "guide to", "explained:", "wordle", "crossword", "sudoku",
    "horoscope", "quiz", "puzzle", "brain teaser",
    # Opinion / Editorial
    "opinion:", "editorial:", "op-ed", "letters to the editor", "perspective:", "analysis:",
    "commentary:", "view:", "my take", "why i",
    # Irrelevant
    "royal family live", "meghan markle live", "harry and meghan live", # Live blogs often spammy
    "not coming to the us", "uk weather", "london", "manchester"
]
BANNED_REGEX = [re.compile(r'\b' + re.escape(p) + r'\b', re.IGNORECASE) for p in BANNED_PHRASES]

# B. Category Keywords (Weighted Categorization)
CATEGORY_KEYWORDS = {
    "Politics": [
        "congress", "senate", "house of representatives", "white house", "president", "biden", "trump",
        "harris", "republican", "democrat", "gop", "election", "campaign", "voter", "ballot", "primary",
        "caucus", "supreme court", "scotus", "legislation", "bill", "veto", "impeachment", "capitol hill",
        "pentagon", "state department", "treasury", "secretary of state", "governor", "mayor", "senator",
        "congressman", "congresswoman", "speaker of the house", "minority leader", "majority leader"
    ],
    "Crime & Legal": [
        "police", "arrest", "suspect", "shooting", "gunman", "murder", "homicide", "kill", "victim",
        "court", "judge", "jury", "trial", "verdict", "guilty", "acquitted", "sentenced", "prison",
        "jail", "inmate", "prosecutor", "attorney", "lawsuit", "suing", "sued", "investigation", "fbi",
        "dea", "atf", "sheriff", "officer", "crime", "criminal", "stabbing", "assault", "robbery",
        "burglary", "theft", "fraud", "scam", "charges", "indictment", "pleaded", "testimony"
    ],
    "Sports": [
        "nfl", "nba", "mlb", "nhl", "mls", "football", "basketball", "baseball", "hockey", "soccer",
        "quarterback", "touchdown", "super bowl", "world series", "stanley cup", "playoffs", "championship",
        "tournament", "league", "team", "coach", "athlete", "player", "score", "win", "loss", "defeat",
        "victory", "medal", "olympics", "world cup", "grand slam", "wimbledon", "us open", "masters",
        "pga", "nascar", "f1", "formula 1", "boxing", "mma", "ufc", "stadium", "arena", "draft", "trade"
    ],
    "Entertainment": [
        "movie", "film", "cinema", "actor", "actress", "director", "producer", "hollywood", "celebrity",
        "star", "fame", "famous", "oscar", "academy award", "grammy", "emmy", "golden globe", "tony award",
        "festival", "premiere", "box office", "trailer", "streaming", "netflix", "hulu", "disney", "hbo",
        "series", "show", "season", "episode", "cast", "concert", "music", "song", "album", "singer",
        "musician", "band", "artist", "comedy", "drama", "thriller", "horror", "romance", "sctv"
    ],
    "Royals": [
        "royal family", "king charles", "queen camilla", "prince william", "princess kate", "middleton",
        "prince harry", "meghan markle", "duke of sussex", "duchess of sussex", "buckingham palace",
        "kensington palace", "windsor", "monarchy", "coronation", "jubilee"
    ],
    "Breaking News": [
        "breaking", "urgent", "developing story", "live updates", "just in", "alert", "emergency",
        "disaster", "hurricane", "tornado", "earthquake", "wildfire", "flood", "explosion", "crash"
    ]
}

# C. US Relevance Keywords (Huge Expansion)
US_RELEVANCE_TERMS = set([
    # Geography
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "washington dc", "d.c.", "nyc", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
    "san antonio", "san diego", "dallas", "san jose", "austin", "jacksonville", "fort worth",
    "columbus", "indianapolis", "charlotte", "san francisco", "seattle", "denver", "nashville",
    "oklahoma city", "el paso", "boston", "portland", "las vegas", "detroit", "memphis", "louisville",
    "baltimore", "milwaukee", "albuquerque", "tucson", "fresno", "sacramento", "mesa", "atlanta",
    "kansas city", "colorado springs", "miami", "raleigh", "omaha", "long beach", "virginia beach",
    "oakland", "minneapolis", "tulsa", "arlington", "tampa", "new orleans", "wichita", "cleveland",
    # Government/Inst
    "fbi", "cia", "nsa", "irs", "cdc", "fda", "epa", "dhs", "dod", "doj", "nasa", "fema", "tsa",
    "fed", "federal reserve", "congress", "senate", "pentagon", "white house", "capitol",
    "supreme court", "constitution", "amendment", "bill of rights",
    # Currency/Econ
    "dollar", "usd", "wall street", "nasdaq", "dow jones", "s&p 500", "nyse", "economy", "inflation",
    # General
    "united states", "usa", "america", "american", "americans", "national"
])

NEGATIVE_TERMS = set([
    "uk", "united kingdom", "britain", "british", "england", "english", "scotland", "scottish",
    "wales", "welsh", "ireland", "irish", "london", "manchester", "liverpool", "birmingham",
    "parliament", "westminster", "downing street", "rishi sunak", "keir starmer", "tory", "labour",
    "brexit", "nhs", "bbc", "sky", "telegraph", "premier league", "cricket", "rugby", "euro",
    "eu", "european union", "australia", "canada", "india", "china", "russia", "ukraine", "gaza"
])

# ===== Section: Utility Functions =====

def log(tag, msg, color=Col.RESET):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{timestamp}] [{tag.upper()}] {msg}{Col.RESET}")

def load_json(filepath, default_factory=dict):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except:
            pass
    return default_factory()

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

def normalize_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'[^\w\s]', '', text)
    return text.lower().strip()

def get_content_hash(title, summary):
    raw = f"{normalize_text(title)}{normalize_text(summary)}"
    return hashlib.md5(raw.encode('utf-8')).hexdigest()

# ===== Section: Data Management =====

class DataManager:
    def __init__(self):
        self.history = self.load_dedup()
        self.metrics = load_json(METRICS_FILE)
        self.posted_this_run = set()

    def load_dedup(self):
        history = []
        now = datetime.now(timezone.utc)
        retention_delta = timedelta(days=HISTORY_RETENTION_DAYS)
        
        if os.path.exists(DEDUP_FILE):
            try:
                with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
                    for line in f:
                        parts = line.strip().split('|')
                        if len(parts) >= 4:
                            # Format: timestamp|url|title|hash
                            try:
                                ts = dateparser.parse(parts[0])
                                if not ts.tzinfo: ts = ts.replace(tzinfo=timezone.utc)
                                
                                # Cleanup: Only keep recent
                                if now - ts < retention_delta:
                                    history.append({
                                        'timestamp': ts,
                                        'url': parts[1],
                                        'title': parts[2],
                                        'hash': parts[3]
                                    })
                            except: continue
            except Exception as e:
                log("DB", f"Error loading history: {e}", Col.RED)
        
        # Save back cleaned version immediately
        self.save_dedup_file(history)
        return history

    def save_dedup_file(self, history_list):
        try:
            with open(DEDUP_FILE, 'w', encoding='utf-8') as f:
                for item in history_list:
                    ts_str = item['timestamp'].isoformat()
                    f.write(f"{ts_str}|{item['url']}|{item['title']}|{item['hash']}\n")
        except Exception as e:
            log("DB", f"Error saving history: {e}", Col.RED)

    def is_duplicate(self, url, title, content_hash):
        # 1. URL Check
        norm_url = url.split('?')[0] # Remove query params
        for item in self.history:
            if item['url'].split('?')[0] == norm_url:
                return True, "URL Match"
            if item['hash'] == content_hash:
                return True, "Hash Match"
        
        # 2. Fuzzy Title Match (Historical)
        norm_title = normalize_text(title)
        for item in self.history:
            hist_title = normalize_text(item['title'])
            if difflib.SequenceMatcher(None, norm_title, hist_title).ratio() > 0.85:
                return True, "Hist Fuzzy Match"

        # 3. In-Run Check
        for p_hash in self.posted_this_run:
            if p_hash == content_hash:
                return True, "In-Run Duplicate"
            
        return False, None

    def add_post(self, url, title, content_hash, source, category):
        entry = {
            'timestamp': datetime.now(timezone.utc),
            'url': url,
            'title': title,
            'hash': content_hash
        }
        self.history.append(entry)
        self.posted_this_run.add(content_hash)
        self.save_dedup_file(self.history)
        
        # Update Metrics
        if "sources" not in self.metrics: self.metrics["sources"] = {}
        if "categories" not in self.metrics: self.metrics["categories"] = {}
        
        self.metrics["sources"][source] = self.metrics["sources"].get(source, 0) + 1
        self.metrics["categories"][category] = self.metrics["categories"].get(category, 0) + 1
        save_json(METRICS_FILE, self.metrics)

# ===== Section: Analysis & Scraping =====

class Analyzer:
    @staticmethod
    def detect_category(title, summary):
        text = f"{title} {summary}".lower()
        scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
        
        for cat, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', text):
                    # Weighted scoring: Title matches worth 2, Summary 1
                    weight = 2 if kw in title.lower() else 1
                    scores[cat] += weight
        
        # Find max score
        best_cat = max(scores, key=scores.get)
        if scores[best_cat] == 0:
            return "Breaking News", 0 # Default
        
        return best_cat, scores[best_cat]

    @staticmethod
    def calculate_us_score(title, summary):
        text = f"{title} {summary}".lower()
        score = 0
        matched = []
        
        # Positive
        for term in US_RELEVANCE_TERMS:
            if re.search(r'\b' + re.escape(term) + r'\b', text):
                score += 1
                matched.append(term)
        
        # Negative (Soft penalty)
        for term in NEGATIVE_TERMS:
             if re.search(r'\b' + re.escape(term) + r'\b', text):
                score -= 1
                
        # Major Event Boost
        boosters = ["dead", "died", "killed", "won", "victory", "champion", "disaster", "crisis"]
        if any(b in text for b in boosters):
            score += 1
            
        return score, list(set(matched))

    @staticmethod
    def is_hard_reject(title, summary):
        text = f"{title} {summary}"
        
        # 1. Check Banned Phrases
        for regex in BANNED_REGEX:
            if regex.search(text):
                return True, f"Banned phrase: {regex.pattern}"
        
        # 2. Check "How To" / Listicle format patterns
        if re.match(r'^\d+\s+(ways|things|reasons)', title, re.IGNORECASE):
            return True, "Listicle detected"
            
        return False, None

class ContentFetcher:
    @staticmethod
    def fetch_meaty_paras(url):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        try:
            response = requests.get(url, headers=headers, timeout=12)
            if response.status_code != 200: return []
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove junk
            for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form', 'iframe', 'ads', 'div.advert']):
                tag.decompose()
                
            paras = soup.find_all('p')
            valid_paras = []
            
            for p in paras:
                text = p.get_text(strip=True)
                # Heuristics for "Meaty" paragraphs
                if len(text) > 80 and not any(x in text.lower() for x in ["click here", "subscribe", "follow us", "read more"]):
                    valid_paras.append(text)
                    if len(valid_paras) >= 3:
                        break
            
            return valid_paras
        except Exception as e:
            log("SCRAPE", f"Failed {url}: {e}", Col.YELLOW)
            return []

# ===== Section: Main Logic =====

class NewsBot:
    def __init__(self):
        self.reddit = praw.Reddit(
            client_id=os.environ['REDDIT_CLIENT_ID'],
            client_secret=os.environ['REDDIT_CLIENT_SECRET'],
            username=os.environ['REDDIT_USERNAME'],
            password=os.environ['REDDITPASSWORD'],
            user_agent='USANewsFlashBot/3.0'
        )
        self.subreddit = self.reddit.subreddit('USANewsFlash')
        self.data = DataManager()
        self.analyzer = Analyzer()
        self.fetcher = ContentFetcher()

    def get_flair_id(self, flair_text):
        try:
            for f in self.subreddit.flair.link_templates:
                if f['text'] == flair_text:
                    return f['id']
        except: pass
        return None

    def run_rss_cycle(self):
        candidates = []
        now = datetime.now(timezone.utc)
        min_time = now - timedelta(hours=TIME_WINDOW_HOURS)

        # 1. Harvest
        for source, url in FEED_SOURCES.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    # Time check
                    dt = None
                    for f in ['published', 'updated', 'created']:
                        if hasattr(entry, f):
                            dt = dateparser.parse(getattr(entry, f))
                            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
                            break
                    
                    if not dt or dt < min_time: continue
                    
                    # Basic Extraction
                    title = html.unescape(entry.title).strip()
                    summary = html.unescape(getattr(entry, 'summary', '')).strip()
                    link = entry.link
                    c_hash = get_content_hash(title, summary)
                    
                    # Dedup Check
                    is_dup, reason = self.data.is_duplicate(link, title, c_hash)
                    if is_dup: continue
                    
                    # Hard Reject Check
                    is_rejected, reason = self.analyzer.is_hard_reject(title, summary)
                    if is_rejected: continue
                    
                    # Scoring & Categorization
                    score, keywords = self.analyzer.calculate_us_score(title, summary)
                    category, cat_score = self.analyzer.detect_category(title, summary)
                    
                    # Threshold: BBC is cleaner, others need higher score
                    threshold = 1 if "BBC" in source else 2
                    
                    if score >= threshold:
                        candidates.append({
                            'source': source,
                            'title': title,
                            'summary': summary,
                            'link': link,
                            'hash': c_hash,
                            'score': score,
                            'keywords': keywords,
                            'category': category,
                            'timestamp': dt
                        })
            except Exception as e:
                log("RSS", f"Error {source}: {e}", Col.RED)

        self.process_candidates(candidates)

    def process_manual_url(self, url):
        log("MANUAL", f"Processing {url}", Col.BLUE)
        # For manual, we don't have RSS metadata, so we must scrape first
        # Simplified: We use the fetcher to get text, title from headers if possible? 
        # Actually easier to use BS4 to get title.
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers)
            soup = BeautifulSoup(resp.content, 'html.parser')
            title = soup.title.string if soup.title else url
            summary = "" # No summary avail
            
            # Create a mock candidate
            candidates = [{
                'source': 'Manual Submission',
                'title': title,
                'summary': summary,
                'link': url,
                'hash': get_content_hash(title, summary),
                'score': 10, # Force post
                'keywords': ['manual'],
                'category': 'Breaking News',
                'timestamp': datetime.now(timezone.utc)
            }]
            self.process_candidates(candidates, manual=True)
        except Exception as e:
            log("MANUAL", f"Failed: {e}", Col.RED)

    def process_candidates(self, candidates, manual=False):
        # Sort by score desc
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        # Round Robin Selection
        selected = []
        source_groups = defaultdict(list)
        for c in candidates:
            source_groups[c['source']].append(c)
            
        active_sources = list(source_groups.keys())
        
        while len(selected) < TARGET_POSTS and active_sources:
            for source in list(active_sources):
                if not source_groups[source]:
                    active_sources.remove(source)
                    continue
                
                # Check max per source
                count_this_source = sum(1 for s in selected if s['source'] == source)
                if count_this_source >= MAX_PER_SOURCE and not manual:
                    active_sources.remove(source)
                    continue
                
                selected.append(source_groups[source].pop(0))
                if len(selected) >= TARGET_POSTS: break
        
        log("SELECT", f"Selected {len(selected)} articles for posting", Col.GREEN)
        
        for article in selected:
            self.post_article(article)

    def post_article(self, article):
        try:
            # 1. Fetch Content
            paras = self.fetcher.fetch_meaty_paras(article['link'])
            if not paras:
                # Fallback to summary if scrape fails
                paras = [article['summary']] if article['summary'] else ["Read the full article at the link."]
            
            # Format Reply
            quote_block = "\n\n".join([f"> {p}" for p in paras[:3]])
            reply_text = (
                f"Source: {article['source']}\n\n"
                f"{quote_block}\n\n"
                f"US Relevance Score: {article['score']} | Keywords: {', '.join(article['keywords'])}\n\n"
                f"[Read more]({article['link']})"
            )
            
            # 2. Submit
            flair_id = self.get_flair_id(article['category'])
            
            submission = self.subreddit.submit(
                title=article['title'],
                url=article['link'],
                flair_id=flair_id
            )
            submission.reply(reply_text)
            
            log("POST", f"Success: {article['title']} [{article['category']}]", Col.GREEN)
            
            # 3. Save
            self.data.add_post(
                article['link'], 
                article['title'], 
                article['hash'], 
                article['source'], 
                article['category']
            )
            
            time.sleep(5) # Safety delay
            
        except Exception as e:
            log("POST", f"Failed {article['title']}: {e}", Col.RED)

# ===== Section: Entry Point =====

if __name__ == "__main__":
    # Check env vars
    required = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME', 'REDDITPASSWORD']
    if any(var not in os.environ for var in required):
        log("INIT", "Missing env vars", Col.RED)
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument('--manual-url', help='Directly post a specific URL')
    args = parser.parse_args()

    bot = NewsBot()
    
    if args.manual_url:
        bot.process_manual_url(args.manual_url)
    else:
        bot.run_rss_cycle()
