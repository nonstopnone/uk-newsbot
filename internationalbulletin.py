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
import logging
import urllib.parse

# --- File and logging setup ---
for fname in [
    'bot.log', 'posted_records.txt', 'rejected_articles.txt',
    'posted_urls.txt', 'posted_titles.txt', 'posted_content_hashes.txt'
]:
    with open(fname, 'a', encoding='utf-8'):
        os.utime(fname, None)

logging.basicConfig(filename='bot.log', level=logging.INFO, 
                   format='%(asctime)s %(levelname)s %(message)s')

# --- Environment variable check ---
required_env_vars = [
    'REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 
    'REDDIT_USERNAME', 'REDDITPASSWORD'
]
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    print(f"ERROR: Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# --- Reddit API setup ---
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    username=os.environ['REDDIT_USERNAME'],
    password=os.environ['REDDITPASSWORD'],
    user_agent='InternationalBulletinBot/1.0'
)
subreddit = reddit.subreddit('InternationalBulletin')

# --- Deduplication data loading ---
def load_posted(fname):
    d = {}
    if os.path.exists(fname):
        with open(fname, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.split('|')
                if len(parts) != 2: continue
                value, timestamp = parts
                try:
                    d[value] = datetime.fromisoformat(timestamp)
                except Exception:
                    continue
    return d

posted_urls = load_posted('posted_urls.txt')
posted_titles = load_posted('posted_titles.txt')
posted_content_hashes = load_posted('posted_content_hashes.txt')

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
            logging.error(f"Failed to save {fname}: {e}")

# --- RSS feeds ---
feed_sources = {
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters World": "http://feeds.reuters.com/Reuters/worldNews",
    "CNN International": "http://rss.cnn.com/rss/edition_world.rss",
    "AP News": "https://www.apnews.com/hub/apnewsfeed",
    "RT International": "https://www.rt.com/rss/news/"
}
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Keywords and tags ---
PROMO_KEYWORDS = [
    "giveaway", "win", "promotion", "contest", "advert", "sponsor",
    "deal", "offer", "competition", "prize", "free", "discount"
]
BREAKING_KEYWORDS = [
    "breaking", "urgent", "crisis", "disaster", "election", "summit",
    "conflict", "agreement", "protest", "attack", "emergency", "revolution"
]
TAGS = [
    "afghanistan", "albania", "algeria", "andorra", "angola", "argentina", "armenia", "australia", "austria", "azerbaijan",
    "bahamas", "bahrain", "bangladesh", "barbados", "belarus", "belgium", "belize", "benin", "bhutan", "bolivia",
    "bosnia", "botswana", "brazil", "brunei", "bulgaria", "burkina", "burundi", "cambodia", "cameroon", "canada",
    "chad", "chile", "china", "colombia", "comoros", "congo", "costa rica", "croatia", "cuba", "cyprus", "czech",
    "denmark", "djibouti", "dominica", "ecuador", "egypt", "el salvador", "eritrea", "estonia", "eswatini", "ethiopia",
    "fiji", "finland", "france", "gabon", "gambia", "georgia", "germany", "ghana", "greece", "grenada", "guatemala",
    "guinea", "guyana", "haiti", "honduras", "hungary", "iceland", "india", "indonesia", "iran", "iraq", "ireland",
    "israel", "italy", "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kiribati", "korea", "kuwait", "kyrgyzstan",
    "laos", "latvia", "lebanon", "lesotho", "liberia", "libya", "lithuania", "luxembourg", "madagascar", "malawi",
    "malaysia", "maldives", "mali", "malta", "mauritania", "mauritius", "mexico", "moldova", "monaco", "mongolia",
    "montenegro", "morocco", "mozambique", "myanmar", "namibia", "nepal", "netherlands", "new zealand", "nicaragua",
    "niger", "nigeria", "norway", "oman", "pakistan", "palau", "panama", "paraguay", "peru", "philippines", "poland",
    "portugal", "qatar", "romania", "russia", "rwanda", "saint lucia", "samoa", "san marino", "saudi", "senegal",
    "serbia", "seychelles", "singapore", "slovakia", "slovenia", "solomon", "somalia", "south africa", "spain",
    "sri lanka", "sudan", "suriname", "sweden", "switzerland", "syria", "taiwan", "tajikistan", "tanzania", "thailand",
    "togo", "tonga", "trinidad", "tunisia", "turkey", "turkmenistan", "tuvalu", "uganda", "ukraine", "uae", "uk",
    "uruguay", "uzbekistan", "vanuatu", "venezuela", "vietnam", "yemen", "zambia", "zimbabwe",
    "europe", "asia", "africa", "north america", "south america", "oceania", "middle east"
]

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme + "://" + parsed.netloc + parsed.path

def normalize_title(title):
    return ' '.join(title.lower().split())

def get_content_hash(entry):
    content = entry.title + getattr(entry, 'summary', '')
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def is_duplicate(entry):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    now = datetime.now(timezone.utc)
    threshold = timedelta(days=7)
    for container, key in [
        (posted_urls, norm_link),
        (posted_titles, norm_title),
        (posted_content_hashes, content_hash)
    ]:
        if key in container and (now - container[key]) < threshold:
            return True
    return False

def is_promotional(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in PROMO_KEYWORDS)

def breaking_score(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return sum(2 if kw in text else 0 for kw in BREAKING_KEYWORDS)

def is_recent(entry, cutoff):
    pubdate = getattr(entry, 'published', getattr(entry, 'updated', None))
    if not pubdate:
        return True
    try:
        pubdate = datetime.strptime(pubdate, '%a, %d %b %Y %H:%M:%S %z')
        return pubdate >= cutoff
    except ValueError:
        return True

# --- Paragraph quality control ---
BAD_PARAGRAPH_PATTERNS = [
    r'error', r'need to view media', r'video only', r'see video', r'see image', r'watch above',
    r'read more', r'continue reading', r'watch the video', r'click here', r'view gallery'
]

def is_good_paragraph(text):
    if not text or len(text) < 60:  # Stricter: at least 60 chars
        return False
    for pat in BAD_PARAGRAPH_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return False
    alpha_ratio = sum(c.isalpha() for c in text) / max(len(text), 1)
    if alpha_ratio < 0.7:
        return False
    if '.' not in text:
        return False
    # Avoid generic or meta paragraphs
    if re.search(r'(subscribe|follow us|our newsletter|copyright|terms of use)', text, re.IGNORECASE):
        return False
    return True

def get_first_good_paragraphs(paragraphs, count=3):
    good = []
    for p in paragraphs:
        if is_good_paragraph(p):
            good.append(p)
        if len(good) == count:
            break
    return good

def extract_first_three_paragraphs(url):
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = [p.get_text().strip() for p in soup.find_all('p') if p.get_text().strip()]
        good_paragraphs = get_first_good_paragraphs(paragraphs, count=3)
        return '\n\n'.join(good_paragraphs) if good_paragraphs else ""
    except Exception as e:
        logging.warning(f"Failed to extract paragraphs from {url}: {e}")
        return ""

def get_tag(text):
    text_lower = text.lower()
    for tag in TAGS:
        if re.search(r'\b' + re.escape(tag) + r'\b', text_lower):
            return tag.capitalize()
    return "International"

# --- Main logic ---
MAX_POSTS_PER_RUN = 5  # LIMIT TO 5 POSTS PER RUN
hours = 12
now_utc = datetime.now(timezone.utc)
hours_ago = now_utc - timedelta(hours=hours)

recent_entries = []
for source, feed_url in feed_sources.items():
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            continue
        for entry in feed.entries:
            if is_recent(entry, hours_ago) and not is_promotional(entry):
                recent_entries.append((source, entry))
    except Exception as e:
        logging.error(f"Failed to parse feed {feed_url}: {e}")

if not recent_entries:
    print("No recent articles found to post.")
    sys.exit(0)

# Sort by breaking score and recency
recent_entries.sort(key=lambda tup: (breaking_score(tup[1]), getattr(tup[1], 'published_parsed', 0)), reverse=True)
selected_entries = recent_entries[:MAX_POSTS_PER_RUN]

current_posts = []
for source, entry in selected_entries:
    try:
        if is_duplicate(entry):
            with open('rejected_articles.txt', 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now(timezone.utc)} | {source} | {entry.title} | {entry.link} | Reason: duplicate\n")
            continue

        combined_text = entry.title + " " + getattr(entry, "summary", "")
        # Try to extract tag from first good paragraph if possible
        summary = extract_first_three_paragraphs(entry.link)
        tag = None
        if summary:
            first_para = summary.split('\n\n')[0]
            tag = get_tag(first_para)
        if not tag or tag == "International":
            tag = get_tag(combined_text)

        title = html.unescape(entry.title)
        post_title = f"{title} | {tag} news"

        if not summary:
            summary = BeautifulSoup(getattr(entry, "summary", ""), 'html.parser').get_text()
            if not is_good_paragraph(summary):
                with open('rejected_articles.txt', 'a', encoding='utf-8') as f:
                    f.write(f"{datetime.now(timezone.utc)} | {source} | {entry.title} | {entry.link} | Reason: bad fallback summary\n")
                continue

        first_para = summary.split('\n\n')[0] if summary else ""
        if not is_good_paragraph(first_para):
            with open('rejected_articles.txt', 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now(timezone.utc)} | {source} | {entry.title} | {entry.link} | Reason: bad first paragraph\n")
            continue

        # --- Post body formatting ---
        body = f"{summary}\n\nRead more at [source]({entry.link})"
        submission = subreddit.submit(post_title, selftext=body)
        post_link = submission.shortlink

        current_posts.append({'title': post_title, 'post_link': post_link, 'article_url': entry.link})

        norm_link = normalize_url(entry.link)
        norm_title = normalize_title(entry.title)
        content_hash = get_content_hash(entry)
        post_time = datetime.now(timezone.utc)
        posted_urls[norm_link] = post_time
        posted_titles[norm_title] = post_time
        posted_content_hashes[content_hash] = post_time
        save_duplicates()

        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with open('posted_records.txt', 'a', encoding='utf-8') as f:
            f.write(f"{timestamp} | {source} | {post_title} | {post_link} | {entry.link}\n")

        time.sleep(30)  # Respect Reddit rate limits

    except Exception as e:
        logging.error(f"Error posting article from {source}: {entry.title} - {e}")

# --- Display results ---
print("\n--- Posts Created in This Run ---")
if current_posts:
    for post in current_posts:
        print(f"Title: {post['title']}")
        print(f"Reddit Link: {post['post_link']}")
        print(f"Article URL: {post['article_url']}\n")
else:
    print("No posts created in this run.")

print("\n--- Historical Posted Records ---")
if os.path.exists('posted_records.txt'):
    with open('posted_records.txt', 'r', encoding='utf-8') as f:
        for line in f:
            print(line.strip())
else:
    print("No historical posted records found.")
