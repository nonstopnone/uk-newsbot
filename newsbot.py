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
from dateutil import parser as dateparser
from dateutil.parser import ParserError

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler('run_log.txt')]
)
logger = logging.getLogger(__name__)

# ---------------- ENV VARS ----------------
required_env_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME']
REDDIT_PASSWORD = os.environ.get('REDDITPASSWORD') or os.environ.get('REDDIT_PASSWORD')
missing_vars = [v for v in required_env_vars if v not in os.environ] + (['REDDITPASSWORD or REDDIT_PASSWORD'] if not REDDIT_PASSWORD else [])
if missing_vars:
    logger.error(f"Missing environment variables: {missing_vars}")
    sys.exit(1)

REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']

# ---------------- REDDIT INIT ----------------
try:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent='BreakingUKNewsBot/1.4'
    )
    subreddit = reddit.subreddit('BreakingUKNews')
except Exception as e:
    logger.error(f"Failed to initialize Reddit: {e}")
    sys.exit(1)

# ---------------- DEDUP ----------------
DEDUP_FILE = './posted_urls.txt'
DEDUP_DAYS = 7
JACCARD_DUPLICATE_THRESHOLD = 0.45

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip('/'), '', '', ''))

def normalize_text(text):
    if not text:
        return ""
    txt = html.unescape(text)
    txt = re.sub(r'[^\w\s£$€]', '', txt)
    txt = re.sub(r'\s+', ' ', txt).strip().lower()
    return txt

def content_hash(title, summary):
    return hashlib.sha256((normalize_text(title) + " " + normalize_text(summary)).encode('utf-8')).hexdigest()

def jaccard_similarity(a, b):
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def load_dedup(filename=DEDUP_FILE):
    urls, titles, hashes = set(), set(), set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
    kept_lines = []
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                parts = line.split('|', 3)
                if len(parts) != 4:
                    continue
                ts_s, url, title, h = parts
                try:
                    ts = dateparser.parse(ts_s)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        urls.add(url)
                        titles.add(title)
                        hashes.add(h)
                        kept_lines.append(line + '\n')
                except (ValueError, ParserError):
                    continue
    # rewrite file to remove old entries
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(kept_lines)
    logger.info(f"Loaded {len(urls)} dedup entries")
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup()

def is_duplicate(entry):
    url = normalize_url(getattr(entry, 'link', ''))
    title_norm = normalize_text(getattr(entry, 'title', ''))
    h = content_hash(getattr(entry, 'title', ''), getattr(entry, 'summary', ''))
    if url in posted_urls:
        return True, "Duplicate URL"
    if h in posted_hashes:
        return True, "Duplicate HASH"
    for pt in posted_titles:
        if jaccard_similarity(title_norm, pt) >= JACCARD_DUPLICATE_THRESHOLD:
            return True, "Duplicate Title"
    return False, ""

def add_to_dedup(entry):
    ts = datetime.now(timezone.utc).isoformat()
    url = normalize_url(getattr(entry, 'link', ''))
    title = normalize_text(getattr(entry, 'title', ''))
    h = content_hash(getattr(entry, 'title', ''), getattr(entry, 'summary', ''))
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{ts}|{url}|{title}|{h}\n")
    posted_urls.add(url)
    posted_titles.add(title)
    posted_hashes.add(h)
    logger.info(f"Added to dedup: {title}")

# ---------------- ARTICLE PARSING ----------------
def extract_first_paragraphs(url, n=3):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in paras:
            pl = p.lower()
            if 'view in browser' in pl or 'open in your browser' in pl:
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', pl):
                continue
            if 'copyright' in pl or '(c)' in pl or '©' in pl:
                continue
            filtered.append(p)
            if len(filtered) >= n:
                break
        while len(filtered) < n:
            filtered.append("")
        return filtered[:n]
    except Exception as e:
        logger.debug(f"extract_first_paragraphs failed for {url}: {e}")
        return [""] * n

def get_full_article_text(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in paras:
            pl = p.lower()
            if 'view in browser' in pl or 'open in your browser' in pl:
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', pl):
                continue
            if 'copyright' in pl or '(c)' in pl or '©' in pl:
                continue
            filtered.append(p)
        return " ".join(filtered)
    except Exception as e:
        logger.debug(f"get_full_article_text failed for {url}: {e}")
        return ""

# ---------------- KEYWORD FILTERS ----------------
PROMO_KW = ["giveaway", "win", "sponsor", "competition", "prize", "free", "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement", "sale", "deal", "black friday", "offer"]
OPINION_KW = ["opinion", "comment", "analysis", "editorial", "viewpoint", "perspective", "column"]
IRRELEVANT_KW = ["mattress", "back pain", "celebrity", "gossip", "fashion", "diet", "product"]

def is_promotional(entry):
    t = html.unescape(getattr(entry, 'title','') + " " + getattr(entry, 'summary','')).lower()
    return any(kw in t for kw in PROMO_KW)

def is_opinion(entry):
    t = html.unescape(getattr(entry, 'title','') + " " + getattr(entry, 'summary','')).lower()
    return any(kw in t for kw in OPINION_KW)

def is_irrelevant(entry):
    t = html.unescape(getattr(entry, 'title','') + " " + getattr(entry, 'summary','')).lower()
    return any(kw in t for kw in IRRELEVANT_KW)

# ---------------- UK RELEVANCE ----------------
UK_KW = {"uk":5, "britain":5, "parliament":6, "downing street":6, "prime minister":6, "nhs":5, "police":4}
NEGATIVE_KW = {"usa":-3, "biden":-3, "trump":-3, "australia":-2}

def calculate_uk_score(text):
    score = 0
    matched = {}
    t = text.lower()
    for kw, w in UK_KW.items():
        c = len(re.findall(r'\b'+re.escape(kw)+r'\b', t))
        if c:
            score += w*c
            matched[kw] = c
    for kw, w in NEGATIVE_KW.items():
        c = len(re.findall(r'\b'+re.escape(kw)+r'\b', t))
        if c:
            score += w*c
            matched[f"negative:{kw}"] = c
    return score, matched

# ---------------- CATEGORISATION ----------------
CATEGORY_KW = {
    "Politics":["politics","parliament","government","election","minister","mp","prime minister","brexit"],
    "Crime & Legal":["crime","police","court","arrest","trial","charged","sentenced","murder"],
    "Sport":["sport","football","cricket","match","won","defeated","beat","injured"],
    "Culture":["culture","museum","festival","exhibition","book","film","theatre"],
    "Economy":["economy","budget","inflation","bank of england","chancellor"],
    "Immigration":["immigration","asylum","refugee","migrant","home office"]
}
FLAIR_MAPPING = {
    "Politics":"Politics",
    "Crime & Legal":"Crime & Legal",
    "Sport":"Sport",
    "Culture":"Culture",
    "Economy":"Economy",
    "Immigration":"Immigration",
    "Notable International":"Notable International News🌍"
}

def get_category(text):
    counts = {}
    t = text.lower()
    for cat,kws in CATEGORY_KW.items():
        c = sum(len(re.findall(r'\b'+re.escape(kw)+r'\b',t)) for kw in kws)
        if c: counts[cat]=c
    if counts:
        cat = max(counts, key=lambda k: counts[k])
    else:
        cat = "Notable International"
    return cat

# ---------------- POST TO REDDIT ----------------
def post_to_reddit(entry, paragraphs):
    url = getattr(entry,'link','')
    title = getattr(entry,'title','')
    logger.info(f"POSTING: {title}")
    logger.info(f"URL: {url}")

    # UK relevance
    full_text = get_full_article_text(url)
    combined = html.unescape(title + " " + getattr(entry,'summary','') + " " + full_text)
    score, matched_keywords = calculate_uk_score(combined)
    category = get_category(combined)
    flair_text = FLAIR_MAPPING.get(category,"Notable International News🌍")

    try:
        # submit post
        submission = subreddit.submit(title=title, url=url)
        if submission:
            # first reply
            reply_lines = []
            for p in paragraphs:
                if p:
                    short = p.strip()
                    if len(short)>200:
                        short=short[:197]+"..."
                    reply_lines.append("> "+short+"\n")
            reply_lines.append(f"[Read more]({url})")
            reply_lines.append("")
            sorted_kw = sorted([(k,v) for k,v in matched_keywords.items() if not k.startswith("negative:")], key=lambda x:-x[1])[:3]
            formatted = ", ".join(f"{k.upper()} ({v})" for k,v in sorted_kw)
            reply_lines.append(f"Automated Flair: {flair_text} ({score}% confidence)")
            reply_lines.append(f"Detected Keywords: {formatted}")
            reply_lines.append(f"More info | Posted automatically. [Subreddit Wiki](https://www.reddit.com/r/BreakingUKNews/wiki/index)")
            submission.reply("\n".join(reply_lines))
            add_to_dedup(entry)
            logger.info(f"POST SUCCESS: {title}")
    except Exception as e:
        logger.error(f"Failed to post {title}: {e}")

# ---------------- MAIN ----------------
def main():
    feed_sources = {
        "BBC UK":"http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky":"https://feeds.skynews.com/feeds/rss/home.xml",
        "Telegraph":"https://www.telegraph.co.uk/rss.xml"
    }
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    all_entries = []
    for name,url in feed_sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                dt = getattr(entry,'published_parsed', None)
                if not dt:
                    dt = getattr(entry,'updated_parsed', None)
                if dt:
                    published = datetime(*dt[:6], tzinfo=timezone.utc)
                    if one_hour_ago <= published <= now + timedelta(minutes=5):
                        all_entries.append(entry)
        except Exception as e:
            logger.error(f"Failed to load feed {name}: {e}")

    for entry in all_entries:
        dup, reason = is_duplicate(entry)
        if dup:
            logger.info(f"SKIPPED DUPLICATE: {getattr(entry,'title','')}")
            continue
        if is_promotional(entry) or is_opinion(entry) or is_irrelevant(entry):
            logger.info(f"SKIPPED CONTENT FILTER: {getattr(entry,'title','')}")
            continue
        paragraphs = extract_first_paragraphs(getattr(entry,'link',''))
        post_to_reddit(entry, paragraphs)

if __name__=="__main__":
    main()
