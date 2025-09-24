import feedparser
import praw
import time
import logging
import re
import html
from collections import defaultdict
from urllib.parse import urlparse
from difflib import SequenceMatcher
import requests
from bs4 import BeautifulSoup

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Reddit API setup
reddit = praw.Reddit(
    client_id='YOUR_CLIENT_ID',
    client_secret='YOUR_CLIENT_SECRET',
    user_agent='YOUR_USER_AGENT',
    username='YOUR_USERNAME',
    password='YOUR_PASSWORD'
)
subreddit = reddit.subreddit('YOUR_SUBREDDIT')

# RSS feeds
RSS_FEEDS = [
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://www.theguardian.com/uk/rss',
    'http://feeds.skynews.com/feeds/rss/uk.xml',
    'http://feeds.skynews.com/feeds/rss/world.xml',
    'https://www.telegraph.co.uk/rss.xml'
]

# Keyword definitions
UK_KEYWORDS = {
    'uk': 5, 'united kingdom': 5, 'britain': 5, 'british': 5, 'england': 4, 'scotland': 4,
    'wales': 4, 'northern ireland': 4, 'london': 3, 'manchester': 3, 'birmingham': 3,
    'liverpool': 3, 'edinburgh': 3, 'glasgow': 3, 'cardiff': 3, 'belfast': 3, 'nhs': 4,
    'brexit': 5, 'parliament': 4, 'westminster': 4, 'prime minister': 4, 'government': 3,
    'queen': 4, 'king': 4, 'royal': 4, 'sainsbury\'s': 3, 'tesco': 3, 'stonehenge': 3,
    'bbc': 3, 'itv': 3, 'pub': 2, 'county': 2, 'ireland': 2
}

NEGATIVE_KEYWORDS = {
    'negative:trump': -3, 'negative:usa': -3, 'negative:america': -3, 'negative:united states': -3,
    'negative:biden': -3, 'negative:china': -3, 'negative:russia': -3, 'negative:india': -3,
    'negative:australia': -2, 'negative:new york': -2, 'negative:california': -2,
    'negative:texas': -2, 'negative:united nations': -2, 'negative:hollywood': -2,
    'negative:olympics': -2, 'negative:fbi': -2, 'negative:jd vance': -2
}

CATEGORY_KEYWORDS = {
    'Politics': ['government', 'parliament', 'prime minister', 'brexit', 'westminster', 'policy', 'election'],
    'Crime & Legal': ['police', 'crime', 'court', 'arrest', 'murder', 'trial', 'investigation'],
    'Business & Economy': ['economy', 'business', 'market', 'finance', 'bank', 'sainsbury\'s', 'tesco'],
    'Health': ['nhs', 'hospital', 'health', 'doctor', 'vaccine', 'disease'],
    'Culture': ['art', 'music', 'film', 'theatre', 'festival', 'british', 'royal'],
    'Sports': ['football', 'cricket', 'rugby', 'tennis', 'athletics'],
    'Environment': ['climate', 'environment', 'pollution', 'wildlife', 'stonehenge'],
    'Technology': ['tech', 'internet', 'cyber', 'ai', 'software']
}

FLAIR_MAPPING = {
    'Politics': 'Politics🗳️',
    'Crime & Legal': 'Crime & Legal⚖️',
    'Business & Economy': 'Business & Economy💰',
    'Health': 'Health🏥',
    'Culture': 'Culture🎭',
    'Sports': 'Sports⚽',
    'Environment': 'Environment🌳',
    'Technology': 'Technology💻',
    'Notable International': 'Notable International News🌍'
}

WHITELISTED_DOMAINS = [
    'bbc.co.uk', 'theguardian.com', 'telegraph.co.uk', 'sky.com', 'itv.com'
]

# Deduplication storage
posted_urls = set()
posted_titles = []

def is_duplicate(entry):
    """Check if an article is a duplicate based on URL or title similarity."""
    url = entry.link
    title = entry.title
    if url in posted_urls:
        logger.info(f"Skipped duplicate article (Duplicate URL): {title}")
        return True
    for posted_title in posted_titles:
        similarity = SequenceMatcher(None, title.lower(), posted_title.lower()).ratio()
        if similarity > 0.9:
            logger.info(f"Skipped duplicate article (Duplicate Title (Fuzzy Match)): {title}")
            return True
    return False

def is_promotional(entry):
    """Check if an article is promotional or an opinion piece."""
    promotional_keywords = ['best', 'top', 'review', 'guide', 'how to', 'opinion', 'editorial']
    title = entry.title.lower()
    if any(keyword in title for keyword in promotional_keywords):
        logger.info(f"Skipped promotional article: {title}")
        return True
    return False

def get_full_article_text(url):
    """Fetch full article text using requests and BeautifulSoup."""
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = soup.find_all('p')
        full_text = ' '.join([p.get_text().strip() for p in paragraphs])
        return full_text[:2000]  # Limit to 2000 chars to reduce processing
    except Exception as e:
        logger.error(f"Failed to fetch full text for {url}: {e}")
        return ""

def count_keywords(text, keywords):
    """Count occurrences of keywords in text, case-insensitive."""
    text = text.lower()
    keyword_counts = defaultdict(int)
    for keyword, weight in keywords.items():
        keyword_lower = keyword.lower().replace('negative:', '')
        count = len(re.findall(r'\b' + re.escape(keyword_lower) + r'\b', text))
        if count > 0:
            keyword_counts[keyword] = count
    return keyword_counts

def is_uk_relevant(entry, full_text):
    """Determine if an article is UK-relevant based on keywords."""
    domain = urlparse(entry.link).netloc
    full_combined = (entry.title + " " + (entry.summary or "") + " " + full_text).lower()
    
    matched_keywords = count_keywords(full_combined, {**UK_KEYWORDS, **NEGATIVE_KEYWORDS})
    matched_keywords['whitelisted_domain'] = 1 if any(domain.endswith(wd) for wd in WHITELISTED_DOMAINS) else 0
    
    uk_placename_pattern = r'\b(london|manchester|birmingham|liverpool|edinburgh|glasgow|cardiff|belfast|leeds|bristol)\b'
    matched_keywords['uk_placename_pattern'] = len(re.findall(uk_placename_pattern, full_combined, re.IGNORECASE))
    
    score = sum(UK_KEYWORDS.get(kw, 0) * count for kw, count in matched_keywords.items() if not kw.startswith('negative:'))
    score += sum(NEGATIVE_KEYWORDS.get(kw, 0) * count for kw, count in matched_keywords.items() if kw.startswith('negative:'))
    score += 5 * matched_keywords.get('whitelisted_domain', 0)
    score += 2 * matched_keywords.get('uk_placename_pattern', 0)
    
    threshold = 3 if matched_keywords.get('whitelisted_domain', 0) else 10
    has_uk_keywords = any(kw in matched_keywords for kw in UK_KEYWORDS)
    
    logger.info(f"Article: {entry.title} | Relevance Score: {score} | Matched: {dict(matched_keywords)} | Category: {get_category(entry, full_text)[0]}")
    
    if score < threshold or not has_uk_keywords:
        logger.info(f"Article rejected: {entry.title} (Reason: Score {score} below threshold {threshold} or no UK keywords)")
        return False, score, matched_keywords
    return True, score, matched_keywords

def get_category(entry, full_text):
    """Determine the category of an article based on keywords."""
    full_combined = (entry.title + " " + (entry.summary or "") + " " + full_text).lower()
    matched_cats = defaultdict(lambda: defaultdict(int))
    all_matched_keywords = set()
    all_matched_cats = set()
    
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            count = len(re.findall(r'\b' + re.escape(keyword) + r'\b', full_combined, re.IGNORECASE))
            if count > 0:
                matched_cats[category][keyword] = count
                all_matched_keywords.add(keyword)
                all_matched_cats.add(category)
    
    cat_scores = {cat: sum(matched_cats[cat].values()) for cat in matched_cats}
    category = max(cat_scores, key=cat_scores.get, default='Notable International') if cat_scores else 'Notable International'
    return category, list(all_matched_keywords), matched_cats, all_matched_cats

def get_relevance_level(score, matched_keywords):
    """Determine the UK relevance level."""
    if score >= 20:
        return "High"
    elif score >= 10:
        return "Medium"
    return "Low"

def extract_first_paragraphs(url):
    """Extract up to three paragraphs from the article."""
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = [p.get_text().strip() for p in soup.find_all('p')[:3] if p.get_text().strip()]
        return paragraphs
    except Exception as e:
        logger.error(f"Failed to extract paragraphs for {url}: {e}")
        return []

def get_post_title(entry):
    """Generate a concise post title."""
    title = html.unescape(entry.title.strip())
    return title[:280]  # Reddit title limit

def add_to_dedup(entry):
    """Add article to deduplication lists."""
    posted_urls.add(entry.link)
    posted_titles.append(entry.title)

def post_to_reddit(entry, score, matched_keywords, retries=3, base_delay=10):
    """Post an article to Reddit with flair and a concise comment."""
    full_text = get_full_article_text(entry.link)
    category, cat_keywords, matched_cats = get_category(entry, full_text)
    flair_text = FLAIR_MAPPING.get(category, "Notable International News🌍")
    flair_id = None
    try:
        for flair in subreddit.flair.link_templates:
            if flair['text'] == flair_text:
                flair_id = flair['id']
                break
    except Exception as e:
        logger.error(f"Failed to fetch flairs: {e}")

    for attempt in range(retries):
        try:
            post_title = get_post_title(entry)
            submission = subreddit.submit(
                title=post_title,
                url=entry.link,
                flair_id=flair_id
            )
            logger.info(f"Posted: {submission.shortlink}")

            # Comment construction
            paragraphs = extract_first_paragraphs(entry.link)
            reply_lines = []
            for para in paragraphs:
                if para:
                    reply_lines.append("> " + html.unescape(para)[:200])
                    reply_lines.append("")
            reply_lines.append(f"[Read more]({entry.link})")
            reply_lines.append("")
            level = get_relevance_level(score, matched_keywords)
            reply_lines.append(f"**UK Relevance**: {level}")
            reply_lines.append("")
            reply_lines.append("**Reason for Posting**:")
            sorted_uk_keywords = sorted(
                [(kw, count) for kw, count in matched_keywords.items() if not kw.startswith("negative:")],
                key=lambda x: -x[1]
            )
            if sorted_uk_keywords:
                kw_list = ', '.join([f'"{kw}"' for kw, _ in sorted_uk_keywords[:5]])
                reply_lines.append(f"This story was posted due to the presence of UK-relevant keywords: {kw_list}.")
            reply_lines.append(f"**Category**: {flair_text}")
            full_reply = "\n".join(reply_lines)
            submission.reply(full_reply)

            add_to_dedup(entry)
            return True
        except praw.exceptions.RedditAPIException as e:
            if "RATELIMIT" in str(e):
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Rate limit hit, retrying in {delay}s (attempt {attempt + 1}/{retries})")
                time.sleep(delay)
            else:
                logger.error(f"Reddit API error: {e}")
                return False
        except Exception as e:
            logger.error(f"Failed to post: {e}")
            return False
    logger.error(f"Failed to post after {retries} attempts")
    return False

def main():
    """Main function to fetch and post UK-relevant articles."""
    articles = []
    posted_count = 0
    max_posts = 10
    category_counts = defaultdict(int)
    max_per_category = 3

    for feed_url in RSS_FEEDS:
        if posted_count >= max_posts:
            break
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                if posted_count >= max_posts:
                    break
                if is_duplicate(entry) or is_promotional(entry):
                    continue
                full_text = get_full_article_text(entry.link)
                is_relevant, score, matched_keywords = is_uk_relevant(entry, full_text)
                if not is_relevant:
                    continue
                category = get_category(entry, full_text)[0]
                if category_counts[category] >= max_per_category:
                    continue
                articles.append((entry, score, matched_keywords, category))
                category_counts[category] += 1
                if post_to_reddit(entry, score, matched_keywords):
                    posted_count += 1
                    logger.info(f"Successfully posted {posted_count}/{max_posts} articles")
                time.sleep(2)  # Avoid overwhelming the server
        except Exception as e:
            logger.error(f"Error processing feed {feed_url}: {e}")
    
    logger.info(f"Completed posting {posted_count} articles")

if __name__ == "__main__":
    main()
