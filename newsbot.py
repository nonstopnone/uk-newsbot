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
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME'
]
password_var = os.environ.get('REDDITPASSWORD') or os.environ.get('REDDIT_PASSWORD')
missing = [v for v in required_env_vars if v not in os.environ] + (['REDDITPASSWORD or REDDIT_PASSWORD'] if not password_var else [])
if missing:
    logger.error(f"Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)

REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
REDDIT_PASSWORD = password_var

# ---------------- REDDIT INIT ----------------
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD,
    user_agent='BreakingUKNewsBot/1.3'
)
subreddit = reddit.subreddit('BreakingUKNews')

# ---------------- DEDUP CONFIG ----------------
DEDUP_FILE = './posted_urls.txt'
DEDUP_DAYS = 7
JACCARD_DUPLICATE_THRESHOLD = 0.45

def normalize_url(url):
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip('/'), '', '', ''))

def normalize_text(text):
    if not text:
        return ""
    t = html.unescape(text)
    t = re.sub(r'[^\w\s£$€]', '', t)
    t = re.sub(r'\s+', ' ', t).strip().lower()
    return t

def content_hash(title, summary):
    return hashlib.sha256((normalize_text(title) + " " + normalize_text(summary)).encode('utf-8')).hexdigest()

def jaccard_similarity(a, b):
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0

def load_dedup():
    urls, titles, hashes = set(), set(), set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
    kept = []
    if not os.path.exists(DEDUP_FILE):
        return urls, titles, hashes
    with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split('|', 3)
            if len(parts) != 4:
                continue
            ts, url, title, h = parts
            try:
                dt = dateparser.parse(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    urls.add(url)
                    titles.add(title)
                    hashes.add(h)
                    kept.append(line)
            except Exception:
                continue
    with open(DEDUP_FILE, 'w', encoding='utf-8') as f:
        f.writelines(kept)
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup()

def is_duplicate(entry):
    url = normalize_url(entry.link)
    title_norm = normalize_text(entry.title)
    h = content_hash(entry.title, getattr(entry, 'summary', ''))
    if url in posted_urls:
        return True
    if h in posted_hashes:
        return True
    for pt in posted_titles:
        if jaccard_similarity(title_norm, pt) >= JACCARD_DUPLICATE_THRESHOLD:
            return True
    return False

def add_to_dedup(entry):
    ts = datetime.now(timezone.utc).isoformat()
    url = normalize_url(entry.link)
    title = normalize_text(entry.title)
    h = content_hash(entry.title, getattr(entry, 'summary', ''))
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{ts}|{url}|{title}|{h}\n")
    posted_urls.add(url)
    posted_titles.add(title)
    posted_hashes.add(h)

# ---------------- ARTICLE FETCH ----------------
def extract_first_paragraphs(url, n=3):
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        return paras[:n] + [""] * max(0, n - len(paras))
    except Exception:
        return [""] * n

# ---------------- REDDIT POST + FIRST REPLY ----------------
def post_to_reddit(entry, score, matched_keywords, category, paragraphs):
    flair_text = category
    flair_id = None
    for flair in subreddit.flair.link_templates:
        if flair.get('text') == flair_text:
            flair_id = flair.get('id')
            break

    submission = subreddit.submit(
        title=html.unescape(entry.title),
        url=entry.link,
        flair_id=flair_id
    )

    confidence = min(100, max(50, score * 10))

    reply_lines = []
    for para in paragraphs:
        if para:
            reply_lines.append("> " + (para[:197] + "..." if len(para) > 200 else para))
            reply_lines.append("")

    reply_lines.append(f"[Read more]({entry.link})")
    reply_lines.append("")
    reply_lines.append(f"**Automated Flair:** {flair_text} ({confidence}% confidence)")

    sorted_uk = sorted(
        [(kw, c) for kw, c in matched_keywords.items() if not kw.startswith("negative:")],
        key=lambda x: -x[1]
    )[:5]

    if sorted_uk:
        formatted = ", ".join([kw.upper() for kw, _ in sorted_uk])
        reply_lines.append(f"**Detected Keywords:** {formatted}")

    reply_lines.append("[More info](https://www.reddit.com/r/BreakingUKNews/wiki/index) | Posted automatically.")

    submission.reply("\n".join(reply_lines))
    add_to_dedup(entry)

# ---------------- MAIN ----------------
def main():
    feed = feedparser.parse("http://feeds.bbci.co.uk/news/uk/rss.xml")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=60)

    for entry in feed.entries:
        try:
            dt = dateparser.parse(entry.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if dt < cutoff:
            continue
        if is_duplicate(entry):
            continue

        paragraphs = extract_first_paragraphs(entry.link)
        matched_keywords = {"uk": 1}
        post_to_reddit(entry, 8, matched_keywords, "Politics", paragraphs)
        time.sleep(10)

if __name__ == "__main__":
    main()
