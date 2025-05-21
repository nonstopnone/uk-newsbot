import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import random
import requests.exceptions

# --- Check for required environment variables ---
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME',
    'REDDITPASSWORD'
]
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    print(f"ERROR: Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# --- Reddit API credentials from environment variables ---
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

# --- Load posted URLs ---
posted_urls = set()
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r') as f:
        posted_urls = set(line.strip() for line in f if line.strip())

# --- UK news RSS feeds (all major sources, fixed Sky News URL) ---
feed_urls = [
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://feeds.skynews.com/feeds/rss/home.xml',
    'https://www.itv.com/news/rss',
    'https://www.telegraph.co.uk/rss.xml',
    'https://www.thetimes.co.uk/rss',
]

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Define the breaking news window (last 7 hours) ---
now_utc = datetime.now(timezone.utc)
seven_hours_ago = now_utc - timedelta(hours=7)

def is_recent(entry):
    """Return True if the entry is published within the last 7 hours."""
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return False
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > seven_hours_ago

def is_valid_url(url):
    """Check if the URL is accessible with a HEAD request."""
    try:
        response = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False

def extract_article_paragraphs(url, title):
    """Extract the first three meaningful paragraphs, skipping short 'top line' blurbs."""
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if p.get_text(strip=True)]

        # Filter out paragraphs that are:
        # - very short (likely a blurb, e.g. under 60 chars)
        # - duplicate of the title
        # - all uppercase or 'By ...' lines
        filtered = []
        for p in paragraphs:
            if len(p) < 60:
                continue
            if title.strip().lower() in p.strip().lower():
                continue
            if p.isupper():
                continue
            if p.strip().startswith("By "):
                continue
            filtered.append(p)
            if len(filtered) == 3:
                break

        if filtered:
            return '\n\n'.join(filtered)
        elif paragraphs:
            return '\n\n'.join(paragraphs[:3])
        else:
            return soup.get_text(strip=True)[:500]
    except Exception as e:
        return f"(Could not extract article text: {e})"

# --- Collect all eligible articles from all feeds ---
articles = []
for feed_url in feed_urls:
    try:
        for attempt in range(3):  # Retry up to 3 times
            try:
                feed = feedparser.parse(feed_url)
                if feed.bozo:
                    raise Exception(f"Malformed feed: {feed.bozo_exception}")
                break
            except (requests.exceptions.RequestException, Exception) as e:
                print(f"Retry {attempt+1}/3 for feed {feed_url}: {e}")
                time.sleep(2)
        else:
            print(f"Failed to parse feed {feed_url} after 3 attempts")
            continue
        for entry in feed.entries:
            title = getattr(entry, 'title', '')
            link = getattr(entry, 'link', '')
            if not link or not title or not isinstance(link, str) or not link.startswith(('http://', 'https://')) or not is_valid_url(link):
                print(f"Skipping invalid article: Title={title}, Link={link}")
                continue
            if link in posted_urls:
                continue
            if not is_recent(entry):
                continue
            articles.append({
                "title": title,
                "link": link
            })
    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")

print(f"Collected {len(articles)} articles: {[(a['title'], a['link']) for a in articles]}")

# --- Shuffle to ensure a mix of sources ---
random.shuffle(articles)

# --- Open log file for this run ---
log_filename = f"run_log_{now_utc.strftime('%Y%m%d_%H%M%S')}.txt"
with open(log_filename, 'w', encoding='utf-8') as log_file:
    new_posts = 0
    failed_posts = 0
    for article in articles:
        title = article["title"]
        link = article["link"]

        quote = extract_article_paragraphs(link, title)
        if quote.startswith("(Could not extract article text"):
            print(f"Skipping post due to failed article extraction: Title={title}, Link={link}")
            log_file.write(f"SKIPPED: Failed extraction - Title={title}, Link={link}\n")
            continue
        body = (
            f"{quote}\n\n"
            f"*Quoted from the link*\n\n"
            f"**What are your thoughts on this story? Join the discussion in the comments.**"
        )

        # --- Format title as requested: no space before pipe, one after ---
        post_title = f"{title}| UK News"

        # --- Log the post attempt ---
        log_file.write("="*60 + "\n")
        log_file.write(f"TITLE: {post_title}\n")
        log_file.write(f"LINK: {link}\n")
        log_file.write(f"BODY:\n{body}\n")
        log_file.write("="*60 + "\n\n")

        if not link or not post_title or not body.strip() or not isinstance(link, str) or not link.startswith(('http://', 'https://')) or not is_valid_url(link):
            print(f"Skipping post due to invalid data: Title={post_title}, Link={link}, Body={body[:50]}...")
            log_file.write(f"SKIPPED: Invalid data - Title={post_title}, Link={link}, Body={body[:50]}...\n")
            continue

        try:
            # Try posting as a link post with body
            submission = subreddit.submit(post_title, url=link, selftext=body)
            print(f"Posted link post to Reddit: {post_title}")
        except praw.exceptions.APIException as e:
            print(f"Reddit API error: {e}, Error Type: {e.error_type}, Message: {str(e)}")
            log_file.write(f"FAILED TO POST: {e}, Error Type: {e.error_type}, Message: {str(e)}\n\n")
            if e.error_type == 'BAD_REQUEST' and 'selftext' in str(e).lower():
                fallback_body = f"[Link to article]({link})\n\n{body}"
                try:
                    submission = subreddit.submit(post_title, selftext=fallback_body)
                    print(f"Posted self (text) post to Reddit (fallback): {post_title}")
                except Exception as e2:
                    print(f"Fallback post failed: {e2}")
                    log_file.write(f"FALLBACK FAILED: {e2}\n\n")
                    failed_posts += 1
                    continue
            else:
                failed_posts += 1
                continue
        except Exception as e:
            print(f"Unexpected error posting to Reddit: {e}")
            log_file.write(f"UNEXPECTED ERROR: {e}\n\n")
            failed_posts += 1
            continue

        posted_urls.add(link)
        new_posts += 1
        time.sleep(10)  # Avoid Reddit rate limits
        if new_posts >= 5:  # Limit per run
            break

    # --- Save posted URLs ---
    with open('posted_urls.txt', 'w') as f:
        for url in posted_urls:
            f.write(url + '\n')

    log_file.write(f"\nTotal new posts this run: {new_posts}\n")
    log_file.write(f"Total failed posts this run: {failed_posts}\n")

    if new_posts == 0:
        if failed_posts > 0:
            print(f"No new UK breaking news stories posted due to {failed_posts} failed attempts. Check logs for details.")
        else:
            print("No new UK breaking news stories found in the last 7 hours.")
    else:
        print(f"Posted {new_posts} new link posts to Reddit from the last 7 hours.")
