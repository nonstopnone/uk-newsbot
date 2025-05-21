import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import random
import urllib.parse
import difflib
import re
import hashlib
import html
import logging

# Ensure log files exist
for fname in ['bot.log', 'posted_records.txt', 'rejected_articles.txt']:
    with open(fname, 'a', encoding='utf-8'):
        os.utime(fname, None)  # Touch the file to ensure it exists

# Set up logging
logging.basicConfig(filename='bot.log', level=logging.INFO, 
                   format='%(asctime)s %(levelname)s %(message)s')

# Check environment variables
required_env_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 
                    'REDDIT_USERNAME', 'REDDITPASSWORD']
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    print(f"ERROR: Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# Reddit API credentials
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    username=os.environ['REDDIT_USERNAME'],
    password=os.environ['REDDITPASSWORD'],
    user_agent='InternationalBulletinBot/1.0'
)
subreddit = reddit.subreddit('InternationalBulletin')

# Load posted data with error handling
posted_urls = {}
posted_titles = {}
posted_content_hashes = {}
for fname, container in [('posted_urls.txt', posted_urls), 
                        ('posted_titles.txt', posted_titles),
                        ('posted_content_hashes.txt', posted_content_hashes)]:
    if os.path.exists(fname):
        with open(fname, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    logging.warning(f"Empty line in {fname}, skipping")
                    continue
                parts = line.split('|')
                if len(parts) != 2:
                    logging.warning(f"Malformed line in {fname}: {line}, skipping")
                    continue
                value, timestamp = parts
                try:
                    container[value] = datetime.fromisoformat(timestamp)
                except ValueError as e:
                    logging.warning(f"Invalid timestamp in {fname}: {line}, skipping: {e}")
                    continue

# RSS feeds from reputable international sources
feed_sources = {
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters World": "http://feeds.reuters.com/Reuters/worldNews",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "CNN International": "http://rss.cnn.com/rss/edition_world.rss",
    "The Guardian World": "https://www.theguardian.com/world/rss",
    "AP News": "https://www.apnews.com/hub/apnewsfeed",
    "France24": "https://www.france24.com/en/rss",
    "DW World": "https://rss.dw.com/xml/rss_en_world",
    "RT International": "https://www.rt.com/rss/news/",
    "NPR World": "https://www.npr.org/rss/rss.php?id=1004"
}

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# Keywords for impactful news
BREAKING_KEYWORDS = [
    "breaking", "urgent", "crisis", "disaster", "election", "summit",
    "conflict", "agreement", "protest", "attack", "emergency", "revolution"
]

PROMO_KEYWORDS = [
    "giveaway", "win", "promotion", "contest", "advert", "sponsor",
    "deal", "offer", "competition", "prize", "free", "discount"
]

# Tags for countries and regions
TAGS = [
    "afghanistan", "albania", "algeria", "andorra", "angola", "antigua and barbuda", 
    "argentina", "armenia", "australia", "austria", "azerbaijan", "bahamas", "bahrain", 
    "bangladesh", "barbados", "belarus", "belgium", "belize", "benin", "bhutan", "bolivia", 
    "bosnia and herzegovina", "botswana", "brazil", "brunei", "bulgaria", "burkina faso", 
    "burundi", "cabo verde", "cambodia", "cameroon", "canada", "central african republic", 
    "chad", "chile", "china", "colombia", "comoros", "congo", "costa rica", "croatia", 
    "cuba", "cyprus", "czech republic", "denmark", "djibouti", "dominica", "dominican republic", 
    "ecuador", "egypt", "el salvador", "equatorial guinea", "eritrea", "estonia", "eswatini", 
    "ethiopia", "fiji", "finland", "france", "gabon", "gambia", "georgia", "germany", "ghana", 
    "greece", "grenada", "guatemala", "guinea", "guinea-bissau", "guyana", "haiti", "honduras", 
    "hungary", "iceland", "india", "indonesia", "iran", "iraq", "ireland", "israel", "italy", 
    "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kiribati", "korea", "kuwait", 
    "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho", "liberia", "libya", "liechtenstein", 
    "lithuania", "luxembourg", "madagascar", "malawi", "malaysia", "maldives", "mali", "malta", 
    "marshall islands", "mauritania", "mauritius", "mexico", "micronesia", "moldova", "monaco", 
    "mongolia", "montenegro", "morocco", "mozambique", "myanmar", "namibia", "nauru", "nepal", 
    "netherlands", "new zealand", "nicaragua", "niger", "nigeria", "north macedonia", "norway", 
    "oman", "pakistan", "palau", "panama", "papua new guinea", "paraguay", "peru", "philippines", 
    "poland", "portugal", "qatar", "romania", "russia", "rwanda", "saint kitts and nevis", 
    "saint lucia", "saint vincent and the grenadines", "samoa", "san marino", "sao tome and principe", 
    "saudi arabia", "senegal", "serbia", "seychelles", "sierra leone", "singapore", "slovakia", 
    "slovenia", "solomon islands", "somalia", "south africa", "south sudan", "spain", "sri lanka", 
    "sudan", "suriname", "sweden", "switzerland", "syria", "taiwan", "tajikistan", "tanzania", 
    "thailand", "timor-leste", "togo", "tonga", "trinidad and tobago", "tunisia", "turkey", 
    "turkmenistan", "tuvalu", "uganda", "ukraine", "united arab emirates", "united states", 
    "uruguay", "uzbekistan", "vanuatu", "venezuela", "vietnam", "yemen", "zambia", "zimbabwe",
    "european union", "eu", "nato", "united nations", "un", "world health organization", "who",
    "world trade organization", "wto", "g7", "g20", "asean", "african union", "au", "opec",
    "europe", "asia", "africa", "north america", "south america", "australia", "middle east"
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
    
    for container, key in [(posted_urls, norm_link), 
                          (posted_titles, norm_title),
                          (posted_content_hashes, content_hash)]:
        if key in container and (now - container[key]) < threshold:
            return True, f"Duplicate found in {container.__name__}"
    return False, ""

def save_duplicate_files():
    for fname, container in [('posted_urls.txt', posted_urls),
                           ('posted_titles.txt', posted_titles),
                           ('posted_content_hashes.txt', posted_content_hashes)]:
        try:
            with open(fname, 'w', encoding='utf-8') as f:
                for key, timestamp in container.items():
                    f.write(f"{key}|{timestamp.isoformat()}\n")
        except Exception as e:
            logging.error(f"Failed to save {fname}: {e}")

def get_tag(text):
    text_lower = text.lower()
    for tag in TAGS:
        if re.search(r'\b' + re.escape(tag) + r'\b', text_lower):
            return tag.capitalize()
    return "International"

def extract_first_three_paragraphs(url):
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = [p.get_text().strip() for p in soup.find_all('p') if p.get_text().strip()]
        return '\n\n'.join(paragraphs[:3]) if paragraphs else ""
    except Exception as e:
        logging.warning(f"Failed to extract paragraphs from {url}: {e}")
        return ""

def is_recent(entry, cutoff):
    pubdate = getattr(entry, 'published', getattr(entry, 'updated', None))
    if not pubdate:
        return True
    try:
        pubdate = datetime.strptime(pubdate, '%a, %d %b %Y %H:%M:%S %z')
        return pubdate >= cutoff
    except ValueError:
        logging.warning(f"Invalid publication date for entry: {getattr(entry, 'title', 'Unknown')}")
        return True

def is_promotional(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in PROMO_KEYWORDS)

def breaking_score(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return sum(2 if kw in text else 0 for kw in BREAKING_KEYWORDS)

# Main logic
MAX_POSTS_PER_RUN = 20
hours = 12
now_utc = datetime.now(timezone.utc)
hours_ago = now_utc - timedelta(hours=hours)

recent_entries = []
for source, feed_url in feed_sources.items():
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            logging.warning(f"No entries found in feed: {feed_url}")
            continue
        for entry in feed.entries:
            if is_recent(entry, hours_ago) and not is_promotional(entry):
                recent_entries.append((source, entry))
    except Exception as e:
        logging.error(f"Failed to parse feed {feed_url}: {e}")

if not recent_entries:
    logging.info("No recent articles found to post.")
    print("No recent articles found to post.")
    sys.exit(0)

recent_entries.sort(key=lambda tup: breaking_score(tup[1]), reverse=True)
selected_entries = recent_entries[:MAX_POSTS_PER_RUN]

current_posts = []
for source, entry in selected_entries:
    try:
        is_dup, reason = is_duplicate(entry)
        if is_dup:
            logging.info(f"Skipping duplicate from {source}: {entry.title} - {reason}")
            continue

        combined_text = entry.title + " " + getattr(entry, "summary", "")
        tag = get_tag(combined_text)
        title = html.unescape(entry.title)
        post_title = f"{title} | {tag} News"

        summary = extract_first_three_paragraphs(entry.link)
        if not summary:
            summary = BeautifulSoup(getattr(entry, "summary", ""), 'html.parser').get_text()
        if not summary:
            logging.info(f"Skipping article with no summary from {source}: {entry.title}")
            continue

        body = f"{summary}\n\nRead more at [source]({entry.link})"
        submission = subreddit.submit(post_title, selftext=body)
        post_link = submission.shortlink

        logging.info(f"Successfully posted from {source}: {post_title} - {post_link}")
        current_posts.append({'title': post_title, 'post_link': post_link, 'article_url': entry.link})

        norm_link = normalize_url(entry.link)
        norm_title = normalize_title(entry.title)
        content_hash = get_content_hash(entry)
        post_time = datetime.now(timezone.utc)
        posted_urls[norm_link] = post_time
        posted_titles[norm_title] = post_time
        posted_content_hashes[content_hash] = post_time
        save_duplicate_files()

        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open('posted_records.txt', 'a', encoding='utf-8') as f:
                f.write(f"{timestamp} | {source} | {post_title} | {post_link} | {entry.link}\n")
        except Exception as e:
            logging.error(f"Failed to write to posted_records.txt: {e}")

        time.sleep(30)  # Delay to respect Reddit rate limits

    except Exception as e:
        logging.error(f"Error posting article from {source}: {entry.title} - {e}")

# Display results
print("\n--- Posts Created in This Run ---")
if current_posts:
    for post in current_posts:
        print(f"Title: {post['title']}")
        print(f"Reddit Link: {post['post_link']}")
        print(f"Article URL: {post['article_url']}\n")
else:
    print("No posts created in this run.")

print("\n--- Historical Posted Records ---")
try:
    if os.path.exists('posted_records.txt'):
        with open('posted_records.txt', 'r', encoding='utf-8') as f:
            for line in f:
                print(line.strip())
    else:
        print("No historical posted records found.")
except Exception as e:
    print(f"Error reading posted records: {e}")
