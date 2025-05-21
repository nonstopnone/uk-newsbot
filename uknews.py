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
import pycountry

# --- Environment variable check ---
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

# --- Reddit API credentials ---
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

# --- Load posted URLs, titles, content hashes, and timestamps ---
posted_urls = {}
posted_titles = {}
posted_content_hashes = {}
if os.path.exists('posted_timestamps.txt'):
    with open('posted_timestamps.txt', 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                timestamp, url, title, content_hash = line.strip().split('|')
                post_time = datetime.fromisoformat(timestamp)
                posted_urls[url] = post_time
                posted_titles[title] = post_time
                posted_content_hashes[content_hash] = post_time

# Remove entries older than 7 days
now_utc = datetime.now(timezone.utc)
seven_days_ago = now_utc - timedelta(days=7)
posted_urls = {k: v for k, v in posted_urls.items() if v > seven_days_ago}
posted_titles = {k: v for k, v in posted_titles.items() if v > seven_days_ago}
posted_content_hashes = {k: v for k, v in posted_content_hashes.items() if v > seven_days_ago}

def save_duplicate_files():
    try:
        with open('posted_urls.txt', 'w', encoding='utf-8') as f:
            for url in posted_urls:
                f.write(url + '\n')
        with open('posted_titles.txt', 'w', encoding='utf-8') as f:
            for title in posted_titles:
                f.write(title + '\n')
        with open('posted_content_hashes.txt', 'w', encoding='utf-8') as f:
            for ch in posted_content_hashes:
                f.write(ch + '\n')
        with open('posted_timestamps.txt', 'w', encoding='utf-8') as f:
            for url, ts in posted_urls.items():
                title = next((t for t, t_ts in posted_titles.items() if t_ts == ts), "")
                ch = next((c for c, c_ts in posted_content_hashes.items() if c_ts == ts), "")
                f.write(f"{ts.isoformat()}|{url}|{title}|{ch}\n")
    except Exception as e:
        print(f"Error saving duplicate files: {e}")

first_run = not bool(posted_urls)

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    normalized_path = parsed.path.rstrip('/')
    normalized_url = urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        normalized_path, '', '', ''
    ))
    return normalized_url

def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_content_hash(entry):
    summary = getattr(entry, "summary", "")[:100]
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def is_duplicate(entry, title_threshold=0.85):
    if first_run:
        return False, ""
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    if norm_link in posted_urls or content_hash in posted_content_hashes:
        return True, "URL or content hash already posted"
    for posted_title in posted_titles:
        similarity = difflib.SequenceMatcher(None, norm_title, posted_title).ratio()
        if similarity > title_threshold:
            return True, f"Title too similar to existing post (similarity: {similarity:.2f})"
    return False, ""

# --- Major UK news RSS feeds ---
feed_sources = {
    "BBC News": "http://feeds.bbci.co.uk/news/rss.xml",
    "BBC News UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
    "BBC Sport Football": "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
    "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    "Times": "https://www.thetimes.co.uk/rss",
    "ITV": "https://www.itv.com/news/rss",
    "ITV Granada": "https://www.itv.com/news/granada/rss",
    "ITV UTV": "https://www.itv.com/news/utv/rss",
    "ITV West Country": "https://www.itv.com/news/westcountry/rss",
    "Guardian": "https://www.theguardian.com/uk/rss"
}

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Keywords for filtering ---
BREAKING_KEYWORDS = [
    "breaking", "urgent", "just in", "developing", "update", "live", "alert", "emergency", 
    "crisis", "disaster", "catastrophe", "motorway pile-up", "national security", 
    "terror attack", "major incident", "evacuation", "lockdown"
]

UK_KEYWORDS = [
    # Geographic Locations
    "uk", "united kingdom", "britain", "great britain", "england", "scotland", "wales", 
    "northern ireland", "london", "manchester", "birmingham", "glasgow", "edinburgh", 
    "cardiff", "belfast", "liverpool", "leeds", "bristol", "sheffield", "newcastle", 
    "nottingham", "southampton", "portsmouth", "oxford", "cambridge", "yorkshire", 
    "lancashire", "devon", "cornwall", "kent", "sussex", "essex", "surrey", "hampshire", 
    "norfolk", "suffolk", "cumbria", "northumberland", "merseyside", "cheshire", "dorset", 
    "somerset",
    # Government and Institutions
    "uk government", "british government", "parliament", "house of commons", "house of lords", 
    "prime minister", "nhs", "national health service", "home office", "foreign office", 
    "treasury", "ministry of defence", "department for education", "scottish parliament", 
    "senedd", "welsh assembly", "stormont", "northern ireland assembly",
    # Cultural and Historical Terms
    "brexit", "british isles", "british culture", "british history", "monarchy", 
    "commonwealth", "bbc", "british broadcasting corporation", "premier league", "fa cup", 
    "wimbledon", "brit awards", "tea culture", "pub culture",
    # Colloquial Terms
    "brit", "british", "uk-based", "uk-wide", "english", "scottish", "welsh", 
    "northern irish", "londoner", "geordie", "scouser", "mancunian"
]

# Generate international keywords using pycountry
INTERNATIONAL_KEYWORDS = [country.name.lower() for country in pycountry.countries] + [
    # International Organizations and Regions
    "european union", "eu", "nato", "united nations", "un", "world health organization", 
    "who", "world trade organization", "wto", "g7", "g20", "asean", "african union", 
    "au", "opec",
    # Continents and Regions
    "europe", "asia", "africa", "north america", "south america", "australia", "middle east", 
    "south asia", "east asia", "southeast asia", "sub-saharan africa",
    # Major Non-UK Cities
    "new york", "washington", "beijing", "moscow", "paris", "berlin", "tokyo", "delhi", 
    "canberra", "brasilia", "cape town"
]

PROMO_KEYWORDS = [
    "giveaway", "win", "promotion", "contest", "advert", "sponsor", "deal", "offer",
    "competition", "prize", "free", "discount"
]

CATEGORY_KEYWORDS = {
    "Breaking News": [
        "breaking", "urgent", "alert", "emergency", "crisis", "disaster", "catastrophe", 
        "motorway pile-up", "national security", "terror attack", "major incident", 
        "evacuation", "lockdown"
    ],
    "Crime & Legal": [
        "crime", "murder", "homicide", "arrest", "robbery", "burglary", "assault", "police", 
        "metropolitan police", "court", "trial", "judge", "lawsuit", "verdict", "conviction", 
        "acquittal", "fraud", "manslaughter", "inquest", "coroner", "prosecution", 
        "defendant", "bail", "sentencing"
    ],
    "Sport": [
        "sport", "football", "premier league", "championship", "fa cup", "cricket", 
        "test match", "ashes", "rugby", "six nations", "tennis", "wimbledon", "athletics", 
        "olympics", "commonwealth games", "cyclist", "grand national", "cheltenham festival", 
        "boxing", "f1", "formula one"
    ],
    "Royals": [
        "royal", "monarch", "queen", "king", "prince", "princess", "duke", "duchess", 
        "buckingham palace", "windsor", "jubilee", "coronation", "royal family", 
        "kate middleton", "prince william", "prince harry", "meghan markle", "king charles"
    ],
    "Culture": [
        "arts", "music", "film", "cinema", "theatre", "west end", "festival", 
        "edinburgh fringe", "glastonbury", "heritage", "literary", "booker prize", 
        "tv series", "british television", "bbc drama", "art exhibition", "national gallery", 
        "tate modern"
    ],
    "Immigration": [
        "immigration", "asylum", "migrant", "refugee", "border", "home office", 
        "channel crossing", "deportation", "visa", "citizenship", "illegal immigration", 
        "rwanda policy", "small boats"
    ],
    "Politics": [
        "parliament", "election", "general election", "by-election", "government", "policy", 
        "legislation", "bill", "house of commons", "house of lords", "prime minister", 
        "chancellor", "cabinet", "tory", "labour", "conservative", "liberal democrats", 
        "snp", "plaid cymru", "dup", "sinn fein"
    ],
    "Economy": [
        "economy", "finance", "business", "taxes", "budget", "employment", "unemployment", 
        "inflation", "cost of living", "energy prices", "retail", "bank of england", 
        "interest rates", "gdp", "trade deficit", "pound sterling"
    ],
    "Notable International News": [
        "international", "global", "foreign", "uk-us", "un climate", "cop conference", 
        "summit", "geopolitics", "sanctions", "war", "conflict", "peace talks"
    ],
    "Trade and Diplomacy": [
        "trade", "diplomacy", "eu", "brexit", "uk-eu", "foreign policy", "ambassador", 
        "trade deal", "export", "import", "tariffs", "bilateral agreement"
    ],
    "National Newspapers Front Pages": [
        "front pages", "newspaper", "telegraph", "guardian", "times", "daily mail", 
        "sun", "mirror", "express", "independent", "ft", "financial times"
    ]
}

# --- Flair mapping ---
FLAIR_MAPPING = {
    "Breaking News": "Breaking News",
    "Crime & Legal": "Crime & Legal",
    "Sport": "Sport",
    "Royals": "Royals",
    "Culture": "Culture",
    "Immigration": "Immigration",
    "Politics": "Politics",
    "Economy": "Economy",
    "Notable International News": "Notable International News",
    "Trade and Diplomacy": "Trade and Diplomacy",
    "National Newspapers Front Pages": "National Newspapers Front Pages",
    None: "No Flair"
}

def extract_first_three_paragraphs(url):
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = []
        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 40:
                paragraphs.append(text)
            if len(paragraphs) == 3:
                break
        if paragraphs:
            return '\n\n'.join(paragraphs)
        else:
            return soup.get_text(strip=True)[:500]
    except Exception as e:
        return ""

def is_uk_relevant(entry):
    # Get title, summary, and article content
    title = entry.title.lower()
    summary = getattr(entry, "summary", "").lower()
    article_text = extract_first_three_paragraphs(entry.link).lower()
    combined_text = title + " " + summary + " " + article_text

    # Count UK and international keywords
    uk_count = sum(combined_text.count(kw) for kw in UK_KEYWORDS)
    intl_count = sum(combined_text.count(kw) for kw in INTERNATIONAL_KEYWORDS)
    
    # Require at least two distinct UK keywords or one high-frequency keyword
    distinct_uk_keywords = len([kw for kw in UK_KEYWORDS if kw in combined_text])
    max_uk_freq = max((combined_text.count(kw) for kw in UK_KEYWORDS), default=0)
    
    # Log rejection if not UK-relevant
    if distinct_uk_keywords < 2 and max_uk_freq < 3:
        log_rejection(entry.title, entry.link, "Insufficient UK keywords")
        return False
    
    # Reject if international focus dominates unless UK impact is clear
    if intl_count > uk_count and not any(kw in combined_text for kw in ["uk impact", "british", "uk government"]):
        log_rejection(entry.title, entry.link, "International focus dominates")
        return False
    
    return True

def log_rejection(title, url, reason):
    try:
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with open('rejected_articles.txt', 'a', encoding='utf-8') as f:
            f.write(f"{timestamp} | {title} | {url} | {reason}\n")
    except Exception as e:
        print(f"Error logging rejection: {e}")

def is_recent(entry, hours_ago):
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return True  # Assume recent for live pages or missing timestamps
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > hours_ago

def is_promotional(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in PROMO_KEYWORDS)

def get_category(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None

def breaking_score(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    score = sum(kw in text for kw in BREAKING_KEYWORDS)
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            score += 1
    return score

def check_messages(posted_titles):
    messages = []
    try:
        for message in reddit.inbox.unread(limit=10):
            if isinstance(message, praw.models.Message):
                msg_body = message.body.lower()
                msg_relevant = ('breakinguknews' in msg_body or
                                any(title.lower() in msg_body for title in posted_titles))
                if msg_relevant:
                    messages.append({
                        'subject': message.subject,
                        'body': message.body,
                        'author': message.author.name if message.author else '[deleted]',
                        'time': datetime.fromtimestamp(message.created_utc, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                    })
                message.mark_read()
    except Exception as e:
        print(f"Error checking messages: {e}")
    return messages

def log_to_records(timestamp, source, title, category, post_link, article_url, comment_link=None, message=None):
    try:
        with open('posted_records.txt', 'a', encoding='utf-8') as f:
            if message:
                f.write(f"{timestamp} | Message | {message['subject']} | {message['author']} | {message['body'][:100]} | {message['time']}\n")
            else:
                comment_field = comment_link if comment_link else "No comment posted"
                f.write(f"{timestamp} | {source} | {title} | {category or 'No Flair'} | {post_link} | {article_url} | {comment_field}\n")
    except Exception as e:
        print(f"Error writing to posted_records.txt: {e}")

def display_posted_records(current_posts):
    print("\n--- Posts Created in This Run ---")
    if current_posts:
        for post in current_posts:
            print(f"Post: {post['title']} | Reddit Link: {post['post_link']} | Article: {post['article_url']}")
    else:
        print("No posts created in this run.")
    print("\n--- Posted Records (Historical) ---")
    try:
        if os.path.exists('posted_records.txt'):
            with open('posted_records.txt', 'r', encoding='utf-8') as f:
                for line in f:
                    print(line.strip())
        else:
            print("No historical posted records found.")
    except Exception as e:
        print(f"Error reading posted_records.txt: {e}")
    print("--- End of Records ---")

def clear_duplicate_files():
    try:
        for file in ['posted_urls.txt', 'posted_titles.txt', 'posted_content_hashes.txt', 'posted_timestamps.txt']:
            if os.path.exists(file):
                os.remove(file)
        print("Cleared duplicate tracking files to allow new posts.")
    except Exception as e:
        print(f"Error clearing duplicate files: {e}")

# --- Gather recent entries (try 12 hours, fallback to 48 hours) ---
def gather_entries(hours):
    now_utc = datetime.now(timezone.utc)
    hours_ago = now_utc - timedelta(hours=hours)
    recent_entries = []
    for source, feed_url in feed_sources.items():
        try:
            feed = feedparser.parse(feed_url)
            source_entries = [
                entry for entry in feed.entries
                if is_recent(entry, hours_ago) and is_uk_relevant(entry) and not is_promotional(entry)
            ]
            for entry in source_entries:
                category = get_category(entry)
                recent_entries.append((source, entry, category))
        except Exception as e:
            print(f"Error processing feed {feed_url}: {e}")
    return recent_entries

# Try 12-hour window first
recent_entries = gather_entries(12)
if len(recent_entries) < 2:
    print(f"Warning: Found only {len(recent_entries)} eligible articles in 12 hours, trying 48-hour window.")
    recent_entries = gather_entries(48)

# --- Sort all entries by breaking-ness ---
recent_entries.sort(key=lambda tup: breaking_score(tup[1]), reverse=True)

# --- Select up to 10 stories ---
selected_entries = recent_entries[:max(10, len(recent_entries))]

# --- Posting to Reddit ---
current_posts = []
posted_titles_for_check = set()
attempted_posts = 0
override_duplicates = len(recent_entries) < 2
if override_duplicates:
    print("Warning: Too few eligible articles, overriding duplicate checks to ensure 2 posts.")

for source, entry, category in selected_entries:
    is_dup, reason = is_duplicate(entry) if not override_duplicates else (False, "")
    if is_dup:
        print(f"Skipping duplicate: {reason}")
        continue
    posted_titles_for_check.add(html.unescape(entry.title))
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)

    # Assign flair
    flair_text = FLAIR_MAPPING.get(category, "No Flair")
    flair_id = None
    try:
        for flair in subreddit.flair.link_templates:
            if flair['text'] == flair_text:
                flair_id = flair['id']
                break
    except Exception as e:
        print(f"Warning: Could not retrieve flair templates: {e}")

    # Decode HTML entities in the title and append "| UK News"
    clean_title = html.unescape(entry.title) + " | UK News"
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        submission = subreddit.submit(
            title=clean_title,
            url=entry.link,
            flair_id=flair_id
        )
        post_link = submission.shortlink
        print(f"Posted: {post_link}")
        current_posts.append({'title': clean_title, 'post_link': post_link, 'article_url': entry.link})
    except Exception as e:
        print(f"Error posting to Reddit: {e}")
        continue

    # Extract and format the body (first 3 paragraphs)
    quoted_body = extract_first_three_paragraphs(entry.link)
    comment_link = None
    if quoted_body:
        quoted_body = html.unescape(quoted_body)
        quoted_lines = [f"> {line}" if line.strip() else "" for line in quoted_body.split('\n')]
        quoted_comment = "\n".join(quoted_lines)
        quoted_comment += f"\n\nRead more at the [source]({entry.link})"
        try:
            comment = submission.reply(quoted_comment)
            comment_link = f"https://www.reddit.com{comment.permalink}"
            print("Added quoted body as comment.")
        except Exception as e:
            print(f"Error posting comment: {e}")

    # Save posted info
    try:
        post_time = datetime.now(timezone.utc)
        posted_urls[norm_link] = post_time
        posted_titles[norm_title] = post_time
        posted_content_hashes[content_hash] = post_time
        save_duplicate_files()
        log_to_records(timestamp, source, clean_title, category, post_link, entry.link, comment_link)
    except Exception as e:
        print(f"Error saving posted info: {e}")

    attempted_posts += 1
    # Stop after posting at least 2 posts
    if len(current_posts) >= 2:
        break

    # Add a delay to avoid rate limits
    time.sleep(30)

# --- Check if at least 2 posts were made ---
if len(current_posts) < 2:
    print(f"ERROR: Only {len(current_posts)} posts made, required at least 2.")
    if not override_duplicates:
        print("Retrying with duplicate override.")
        # Retry with override if not already done
        override_duplicates = True
        for source, entry, category in selected_entries[:2 - len(current_posts)]:
            posted_titles_for_check.add(html.unescape(entry.title))
            norm_link = normalize_url(entry.link)
            norm_title = normalize_title(entry.title)
            content_hash = get_content_hash(entry)

            flair_text = FLAIR_MAPPING.get(category, "No Flair")
            flair_id = None
            try:
                for flair in subreddit.flair.link_templates:
                    if flair['text'] == flair_text:
                        flair_id = flair['id']
                        break
            except Exception as e:
                print(f"Warning: Could not retrieve flair templates: {e}")

            clean_title = html.unescape(entry.title) + " | UK News"
            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            try:
                submission = subreddit.submit(
                    title=clean_title,
                    url=entry.link,
                    flair_id=flair_id
                )
                post_link = submission.shortlink
                print(f"Posted (override): {post_link}")
                current_posts.append({'title': clean_title, 'post_link': post_link, 'article_url': entry.link})
            except Exception as e:
                print(f"Error posting to Reddit (override): {e}")
                continue

            quoted_body = extract_first_three_paragraphs(entry.link)
            comment_link = None
            if quoted_body:
                quoted_body = html.unescape(quoted_body)
                quoted_lines = [f"> {line}" if line.strip() else "" for line in quoted_body.split('\n')]
                quoted_comment = "\n".join(quoted_lines)
                quoted_comment += f"\n\nRead more at the [source]({entry.link})"
                try:
                    comment = submission.reply(quoted_comment)
                    comment_link = f"https://www.reddit.com{comment.permalink}"
                    print("Added quoted body as comment (override).")
                except Exception as e:
                    print(f"Error posting comment (override): {e}")

            try:
                post_time = datetime.now(timezone.utc)
                posted_urls[norm_link] = post_time
                posted_titles[norm_title] = post_time
                posted_content_hashes[content_hash] = post_time
                save_duplicate_files()
                log_to_records(timestamp, source, clean_title, category, post_link, entry.link, comment_link)
            except Exception as e:
                print(f"Error saving posted info (override): {e}")

            time.sleep(30)

# Final check
if len(current_posts) < 2:
    print(f"ERROR: Failed to post 2 articles after all attempts. Clearing duplicate files.")
    clear_duplicate_files()
    display_posted_records(current_posts)
    sys.exit(1)

# --- Check messages after posting ---
messages = check_messages(posted_titles_for_check)
for message in messages:
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    log_to_records(timestamp, "N/A", "N/A", "Message", "N/A", "N/A", None, message)

# --- Display posted records and current run's posts ---
display_posted_records(current_posts)
