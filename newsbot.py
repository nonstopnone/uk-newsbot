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

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler('run_log.txt')]
)
logger = logging.getLogger(__name__)

# ---------------- ENV ----------------
required = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'REDDIT_USERNAME']
missing = [v for v in required if v not in os.environ]
password = os.environ.get('REDDITPASSWORD') or os.environ.get('REDDIT_PASSWORD')

if missing or not password:
    logger.error('Missing Reddit credentials')
    sys.exit(1)

# ---------------- REDDIT ----------------
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    username=os.environ['REDDIT_USERNAME'],
    password=password,
    user_agent='BreakingUKNewsBot/2.0'
)
subreddit = reddit.subreddit('BreakingUKNews')

# ---------------- DEDUP ----------------
DEDUP_FILE = 'posted_urls.txt'
DEDUP_DAYS = 14

def extract_bbc_id(url):
    m = re.search(r'/articles/([a-z0-9]+)', url)
    return m.group(1) if m else None

def canonical_url(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"

def text_hash(text):
    return hashlib.sha256(text.lower().encode('utf-8')).hexdigest()

def load_dedup():
    seen = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)

    if not os.path.exists(DEDUP_FILE):
        return seen

    with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                ts, key = line.strip().split('|', 1)
                dt = dateparser.parse(ts)
                if dt >= cutoff:
                    seen[key] = dt
            except Exception:
                continue
    return seen

def save_dedup(seen):
    with open(DEDUP_FILE, 'w', encoding='utf-8') as f:
        for k, dt in seen.items():
            f.write(f"{dt.isoformat()}|{k}\n")

seen_items = load_dedup()

def is_duplicate(entry):
    url = canonical_url(entry.link)
    bbc_id = extract_bbc_id(url)
    title_h = text_hash(entry.title)

    keys = [
        f"url:{url}",
        f"title:{title_h}"
    ]

    if bbc_id:
        keys.append(f"bbc:{bbc_id}")

    for k in keys:
        if k in seen_items:
            logger.info(f"Duplicate blocked: {k}")
            return True

    return False

def mark_posted(entry):
    now = datetime.now(timezone.utc)
    url = canonical_url(entry.link)
    bbc_id = extract_bbc_id(url)
    title_h = text_hash(entry.title)

    seen_items[f"url:{url}"] = now
    seen_items[f"title:{title_h}"] = now
    if bbc_id:
        seen_items[f"bbc:{bbc_id}"] = now

    save_dedup(seen_items)

# ---------------- CONTENT ----------------
def extract_paragraphs(url, n=3):
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(r.text, 'html.parser')
        paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 50]
        return paras[:n]
    except Exception:
        return []

# ---------------- POST ----------------
def post(entry):
    submission = subreddit.submit(
        title=html.unescape(entry.title),
        url=canonical_url(entry.link),
        flair_text='Politics'
    )

    paras = extract_paragraphs(entry.link)

    reply = []
    for p in paras:
        reply.append('> ' + p)
        reply.append('')

    reply.append('[Read more](' + canonical_url(entry.link) + ')')
    reply.append('')
    reply.append('**Automated Flair:** Politics')
    reply.append('[More info](https://www.reddit.com/r/BreakingUKNews/wiki/index) | Posted automatically.')

    submission.reply('\n'.join(reply))
    mark_posted(entry)

# ---------------- MAIN ----------------
def main():
    feed = feedparser.parse('https://feeds.bbci.co.uk/news/uk/rss.xml')
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=90)

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

        post(entry)
        time.sleep(15)

if __name__ == '__main__':
    main()
