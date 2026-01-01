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

# ---------------- ENV VARS (flexible password name) ----------------
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME'
]
# Accept either REDDITPASSWORD or REDDIT_PASSWORD to be compatible with different deployments
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
try:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent='BreakingUKNewsBot/1.3'
    )
    subreddit = reddit.subreddit('BreakingUKNews')
except Exception as e:
    logger.error(f"Failed to initialize Reddit API: {e}")
    sys.exit(1)

# ---------------- DEDUP CONFIG ----------------
DEDUP_FILE = './posted_urls.txt'
DEDUP_DAYS = 7
JACCARD_DUPLICATE_THRESHOLD = 0.45

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    # canonicalize scheme and host and trim trailing slash in path
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip('/'), '', '', ''))

def normalize_text(text):
    if not text:
        return ""
    txt = html.unescape(text)
    txt = re.sub(r'[^\w\s£$€]', '', txt)
    txt = re.sub(r'\s+', ' ', txt).strip().lower()
    return txt

def content_hash(title, summary):
    base = normalize_text(title) + " " + normalize_text(summary)
    return hashlib.sha256(base.encode('utf-8')).hexdigest()

def jaccard_similarity(a, b):
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def load_dedup(filename=DEDUP_FILE):
    urls, titles, hashes = set(), set(), set()
    kept_lines = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_DAYS)
    if not os.path.exists(filename):
        return urls, titles, hashes
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            # Use maxsplit to preserve titles that might contain pipes
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
    # rewrite file with only kept lines (prune older than cutoff)
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(kept_lines)
    logger.info(f"Loaded {len(urls)} dedup entries (last {DEDUP_DAYS} days)")
    return urls, titles, hashes

posted_urls, posted_titles, posted_hashes = load_dedup()

def is_duplicate(entry):
    try:
        url = normalize_url(entry.link)
    except Exception:
        url = ""
    title_norm = normalize_text(getattr(entry, 'title', ''))
    summary = normalize_text(getattr(entry, 'summary', ''))
    h = content_hash(getattr(entry, 'title', ''), getattr(entry, 'summary', ''))

    if url and url in posted_urls:
        return True, "Duplicate URL"
    if h in posted_hashes:
        return True, "Duplicate HASH"
    for pt in posted_titles:
        if jaccard_similarity(title_norm, pt) >= JACCARD_DUPLICATE_THRESHOLD:
            return True, "Duplicate Title (Jaccard)"
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

# ---------------- ARTICLE FETCH / PARSING ----------------
def extract_first_paragraphs(url, n=3):
    """Return up to n cleaned first paragraphs suitable for the first reply."""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        raw = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in raw:
            pl = p.lower()
            if ('view in browser' in pl) or ('open in your browser' in pl) or re.search(r'open (this|the) (article|page|link)', pl):
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', pl) or re.search(r'(^|\n)\s*by\s+[A-Z][\w\-\'\.]+', p):
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
    """Return concatenation of article paragraphs for scoring."""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in paras:
            pl = p.lower()
            if ('view in browser' in pl) or ('open in your browser' in pl) or re.search(r'open (this|the) (article|page|link)', pl):
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', pl) or re.search(r'(^|\n)\s*by\s+[A-Z][\w\-\'\.]+', p):
                continue
            if 'copyright' in pl or '(c)' in pl or '©' in pl:
                continue
            filtered.append(p)
        return ' '.join(filtered)
    except Exception as e:
        logger.debug(f"get_full_article_text failed for {url}: {e}")
        return ""

# ---------------- KEYWORD FILTERS ----------------
PROMOTIONAL_KEYWORDS = [
    "giveaway", "win", "sponsor", "competition", "prize", "free",
    "discount", "voucher", "promo code", "coupon", "partnered", "advert", "advertisement",
    "sale", "deal", "black friday", "offer"
]
OPINION_KEYWORDS = [
    "opinion", "comment", "analysis", "editorial", "viewpoint", "perspective", "column"
]
IRRELEVANT_KEYWORDS = [
    "mattress", "back pain", "best mattresses", "celebrity", "gossip", "fashion", "diet",
    "workout", "product", "seasonal", "deals", "anniversary", "celebrate", "birthday"
]
SPORTS_PREVIEW_KEYWORDS = [
    "preview", "when is", "how to watch", "start time", "fixtures", "schedule", "tv channel", "where to watch"
]

def is_promotional(entry):
    combined = html.unescape(getattr(entry, 'title', '') + " " + getattr(entry, 'summary', '')).lower()
    if "offer" in combined and any(kw in combined for kw in ["government", "nhs", "policy", "public sector"]):
        return False
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)

def is_opinion(entry):
    combined = html.unescape(getattr(entry, 'title', '') + " " + getattr(entry, 'summary', '')).lower()
    return any(kw in combined for kw in OPINION_KEYWORDS)

def is_irrelevant_fluff(entry):
    combined = html.unescape(getattr(entry, 'title', '') + " " + getattr(entry, 'summary', '')).lower()
    return any(kw in combined for kw in IRRELEVANT_KEYWORDS)

def is_sports_preview(text):
    t = text.lower()
    has_preview = any(kw in t for kw in SPORTS_PREVIEW_KEYWORDS)
    result_keywords = ["won", "wins", "winner", "defeated", "beat", "victory", "champion", "result", "defeats", "beats", "crowned"]
    has_result = any(kw in t for kw in result_keywords)
    return has_preview and not has_result

# ---------------- UK RELEVANCE ----------------
UK_KEYWORDS = {
    "united kingdom": 6, "uk": 5, "britain": 5,
    "parliament": 6, "westminster": 6, "downing street": 6,
    "prime minister": 6, "home office": 5, "nhs": 5,
    "court": 4, "charged": 4, "sentenced": 4, "arrested": 4,
    "police": 4, "met police": 4, "network rail": 3
}
NEGATIVE_KEYWORDS = {
    "washington": -3, "biden": -3, "trump": -3, "us": -3, "america": -3,
    "australia": -2, "canada": -2
}
strong_uk_keywords = ["uk", "britain", "united kingdom", "england", "scotland", "wales", "northern ireland", "parliament", "prime minister"]

def calculate_uk_relevance_score(text):
    score = 0
    matched = {}
    tl = text.lower()
    for kw, w in UK_KEYWORDS.items():
        c = len(re.findall(r'\b' + re.escape(kw) + r'\b', tl))
        if c:
            score += w * c
            matched[kw] = c
    for kw, w in NEGATIVE_KEYWORDS.items():
        c = len(re.findall(r'\b' + re.escape(kw) + r'\b', tl))
        if c:
            score += w * c
            matched[f"negative:{kw}"] = c
    # simple placename heuristic - treat a 'shire' like 'someshire' as positive if not in negative keys
    placenames = re.findall(r'\b(\w+(shire|ton|ham|bridge|ford))\b', tl)
    for pn in set(placenames):
        name = pn[0].lower()
        if name not in matched and f"negative:{name}" not in matched:
            count = len(re.findall(r'\b' + re.escape(name) + r'\b', tl))
            if count:
                score += 2 * count
                matched[name] = count
    return score, matched

def get_relevance_level(score, matched_keywords):
    has_strong = any(k in matched_keywords for k in strong_uk_keywords)
    if score >= 12:
        return "Very High"
    if score >= 8 or has_strong:
        return "High"
    if score >= 5:
        return "Medium"
    if score >= 3:
        return "Low"
    return "Very Low"

# ---------------- CATEGORISATION & FLAIRS ----------------
CATEGORY_KEYWORDS = {
    "Politics": ["politics", "parliament", "government", "election", "minister", "mp", "prime minister", "brexit"],
    "Crime & Legal": ["crime", "police", "court", "arrest", "trial", "charged", "sentenced", "murder"],
    "Sport": ["sport", "football", "cricket", "match", "won", "defeated", "beat", "injured"],
    "Culture": ["culture", "museum", "festival", "exhibition", "book", "film", "theatre"],
    "Economy": ["economy", "budget", "inflation", "bank of england", "chancellor"],
    "Immigration": ["immigration", "asylum", "refugee", "migrant", "home office"]
}
FLAIR_MAPPING = {
    "Politics": "Politics",
    "Crime & Legal": "Crime & Legal",
    "Sport": "Sport",
    "Culture": "Culture",
    "Economy": "Economy",
    "Immigration": "Immigration",
    "Notable International": "Notable International News🌍"
}
priority_order = ["Crime & Legal", "Politics", "Economy", "Immigration", "Sport", "Culture"]

def get_category(combined_text, full_text):
    t = full_text.lower()
    matched = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        counts = {}
        for kw in kws:
            c = len(re.findall(r'\b' + re.escape(kw) + r'\b', t))
            if c:
                counts[kw] = c
        if counts:
            matched[cat] = counts
    score, matched_uk = calculate_uk_relevance_score(combined_text)
    if not matched:
        return "Notable International", {}, {}, [], {}, score, matched_uk
    cat_scores = {cat: sum(m.values()) for cat, m in matched.items()}
    max_score = max(cat_scores.values())
    candidates = [cat for cat, s in cat_scores.items() if s == max_score]
    chosen = min(candidates, key=lambda c: priority_order.index(c) if c in priority_order else len(priority_order))
    # special-case: if Politics content looks foreign and no strong UK term, downgrade
    has_foreign = any(k.startswith("negative:") for k in matched_uk)
    has_strong_uk = any(k in matched_uk for k in strong_uk_keywords)
    if chosen == "Politics" and has_foreign and not has_strong_uk:
        chosen = "Notable International"
    return chosen, matched.get(chosen, {}), {kw: ct for m in matched.values() for kw, ct in m.items()}, list(matched.keys()), matched, score, matched_uk

# ---------------- LOG HELPERS ----------------
def log_rejected(source, entry, reason):
    ts = datetime.now(timezone.utc).isoformat()
    title = getattr(entry, 'title', 'N/A')
    logger.warning(f"[REJECTED] {ts} | {source} | {title} | {reason}")

def log_posted(source, entry, score, category, level, matched_keywords):
    ts = datetime.now(timezone.utc).isoformat()
    title = getattr(entry, 'title', 'N/A')
    top_kw = ', '.join([f"{k.upper()} ({v})" for k, v in sorted({k: v for k, v in matched_keywords.items() if not k.startswith('negative:')}.items(), key=lambda x: -x[1])[:3]])
    reason = f"Passed with {level} relevance, score: {score}, keywords: {top_kw}"
    logger.info(f"[POSTED] {ts} | {source} | {title} | {score} | {category} | {reason}")

def log_error(source, entry, error_msg):
    ts = datetime.now(timezone.utc).isoformat()
    title = getattr(entry, 'title', 'N/A') if entry else "N/A"
    logger.error(f"[ERROR] {ts} | {source} | {title} | {error_msg}")

# ---------------- REDDIT POST + FIRST REPLY ----------------
def post_to_reddit(entry, score, matched_keywords, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, retries=3, base_delay=10):
    flair_text = FLAIR_MAPPING.get(category, "Notable International News🌍")
    flair_id = None
    try:
        for flair in subreddit.flair.link_templates:
            if flair.get('text') == flair_text:
                flair_id = flair.get('id')
                break
    except Exception as e:
        log_error("", entry, f"Failed to fetch flairs: {e}")

    # compute category assignment confidence as fraction of matched keywords for chosen category
    cat_scores = {cat: sum(matched_cats.get(cat, {}).values()) for cat in all_matched_cats} if all_matched_cats else {}
    total = sum(cat_scores.values()) or 1
    chosen_score = cat_scores.get(category, 0)
    confidence = int(100 * chosen_score / total) if total > 0 else 50

    for attempt in range(retries):
        try:
            post_title = html.unescape(getattr(entry, 'title', ''))
            submission = subreddit.submit(title=post_title, url=getattr(entry, 'link', None), flair_id=flair_id)
            logger.info(f"Posted: {getattr(submission, 'shortlink', submission)}")

            # build first reply
            reply_lines = []
            for para in paragraphs:
                if para:
                    short = para.strip()
                    if len(short) > 200:
                        short = short[:197] + "..."
                    reply_lines.append("> " + short)
                    reply_lines.append("")
            # include Read more link
            reply_lines.append(f"[Read more]({getattr(entry, 'link', '')})")
            reply_lines.append("")
            # UK relevance summary
            reply_lines.append("**UK Relevance**")
            sorted_uk = sorted([(kw, count) for kw, count in matched_keywords.items() if not kw.startswith("negative:")], key=lambda x: -x[1])[:3]
            if sorted_uk:
                kw_parts = []
                for kw, count in sorted_uk:
                    times_str = "time" if count == 1 else "times"
                    kw_parts.append(f"{kw.upper()} ({count} {times_str})")
                if len(kw_parts) > 1:
                    formatted = ", ".join(kw_parts[:-1]) + " and " + kw_parts[-1]
                else:
                    formatted = kw_parts[0]
                reply_lines.append(f"This article was posted because the system detected key UK-related terms such as {formatted}, indicating it fits the {flair_text} category and is likely of interest to a UK audience.")
            reply_lines.append(f"Based on this assessment, the system automatically assigned the {flair_text} flair with {confidence}% confidence.")
            reply_lines.append("This was automatically posted by the BreakingUKNews automation system.")
            reply_lines.append("(For more information, see the subreddit wiki.)")

            full_reply = "\n".join(reply_lines)
            try:
                submission.reply(full_reply)
            except Exception as e:
                log_error("", entry, f"Failed to post first reply: {e}")

            add_to_dedup(entry)
            return True
        except praw.exceptions.RedditAPIException as e:
            if "RATELIMIT" in str(e).upper():
                delay = base_delay * (2 ** attempt)
                log_error("", entry, f"Rate limit hit, retrying in {delay}s (attempt {attempt + 1}/{retries})")
                time.sleep(delay)
                continue
            else:
                log_error("", entry, f"Reddit API error: {e}")
                return False
        except Exception as e:
            log_error("", entry, f"Failed to post: {e}")
            return False
    log_error("", entry, f"Failed to post after {retries} attempts")
    return False

# ---------------- MAIN PROCESS ----------------
def get_entry_published_datetime(entry):
    for field in ['published', 'updated', 'created', 'date']:
        if hasattr(entry, field):
            try:
                dt = dateparser.parse(getattr(entry, field))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, ParserError):
                continue
    return None

def main():
    TARGET_POSTS_PER_RUN = 7
    INITIAL_ARTICLES = 30
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml"
    }

    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(minutes=60)

    all_entries = []
    for name, url in feed_sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published_dt = get_entry_published_datetime(entry)
                # allow small future skew
                if published_dt and one_hour_ago <= published_dt <= now + timedelta(minutes=5):
                    all_entries.append((name, entry, published_dt))
        except Exception as e:
            log_error(name, None, f"Error loading feed: {e}")

    logger.info(f"Found {len(all_entries)} entries published in the last 60 minutes.")
    all_entries.sort(key=lambda x: x[2], reverse=True)

    all_articles = []
    category_counts = {cat: 0 for cat in list(CATEGORY_KEYWORDS.keys()) + ["Notable International"]}
    winner_keywords = ["wins", "defeats", "beats", "victory", "champion", "winner", "crowned", "triumphs", "claims title"]

    for name, entry, published_dt in all_entries:
        if len(all_articles) >= INITIAL_ARTICLES:
            break

        dup, reason = is_duplicate(entry)
        if dup:
            log_rejected(name, entry, f"Duplicate: {reason}")
            continue

        if is_promotional(entry):
            log_rejected(name, entry, "Promotional content")
            continue

        if is_opinion(entry):
            log_rejected(name, entry, "Opinion piece")
            continue

        if is_irrelevant_fluff(entry):
            log_rejected(name, entry, "Irrelevant fluff")
            continue

        full_text = get_full_article_text(entry.link)
        if not full_text:
            log_rejected(name, entry, "Failed to fetch full text")
            continue

        full_combined = html.unescape(getattr(entry, 'title', '') + " " + getattr(entry, 'summary', '') + " " + full_text).lower()
        category, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, score, matched_keywords = get_category(full_combined, full_text)

        if category == "Sport":
            if is_sports_preview(full_combined) or not any(kw in full_combined for kw in winner_keywords):
                log_rejected(name, entry, "Sports preview or non-result")
                continue

        has_uk_term = any(not k.startswith("negative:") for k in matched_keywords)
        threshold = 8 if category == "Culture" else {**{k: v for k, v in {**{}}.items()}, **{}} and 5  # conservative default
        # Use CATEGORY_THRESHOLDS from earlier fuller code if defined, otherwise default
        try:
            threshold = globals().get('CATEGORY_THRESHOLDS', {}).get(category, 5)
        except Exception:
            threshold = 5

        if score < threshold or not has_uk_term:
            log_rejected(name, entry, f"Score {score} below threshold {threshold} or no UK terms")
            continue

        negative_matches = [k for k in matched_keywords if k.startswith("negative:")]
        positive_sum = sum(v for k, v in matched_keywords.items() if not k.startswith("negative:"))
        negative_sum = sum(v for k, v in matched_keywords.items() if k.startswith("negative:"))
        if negative_matches and (-negative_sum) > positive_sum * 0.5:
            log_rejected(name, entry, "Foreign dominance")
            continue

        level = get_relevance_level(score, matched_keywords)
        if level not in ["High", "Very High"]:
            log_rejected(name, entry, f"Relevance level {level} too low")
            continue

        paragraphs = extract_first_paragraphs(entry.link)
        norm_title = normalize_text(getattr(entry, 'title', ''))

        if category_counts.get(category, 0) < 3:
            logger.info(f"Selected article: {getattr(entry, 'title', '')} | Score: {score} | Category: {category}")
            all_articles.append((name, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, level))
            category_counts[category] = category_counts.get(category, 0) + 1

    # second-stage dedup within selected candidates
    unique_articles = []
    seen_urls = set()
    seen_titles = set()
    seen_hashes = set()

    for article in all_articles:
        source, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, level = article
        norm_link = normalize_url(getattr(entry, 'link', ''))
        h = content_hash(getattr(entry, 'title', ''), getattr(entry, 'summary', ''))
        is_dup = norm_link in seen_urls or h in seen_hashes
        if not is_dup:
            for st in seen_titles:
                if jaccard_similarity(norm_title, st) >= JACCARD_DUPLICATE_THRESHOLD:
                    is_dup = True
                    break
        if not is_dup:
            unique_articles.append(article)
            seen_urls.add(norm_link)
            seen_titles.add(norm_title)
            seen_hashes.add(h)

    all_articles = unique_articles
    all_articles.sort(key=lambda x: x[2], reverse=True)

    selected_for_posting = []
    temp_category_counts = {cat: 0 for cat in list(CATEGORY_KEYWORDS.keys()) + ["Notable International"]}

    for article in all_articles:
        if len(selected_for_posting) >= TARGET_POSTS_PER_RUN:
            break
        source, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, level = article
        if temp_category_counts.get(category, 0) < 3:
            selected_for_posting.append(article)
            temp_category_counts[category] += 1

    posts_made = 0
    skipped = 0
    for article in selected_for_posting:
        source, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, level = article
        success = post_to_reddit(entry, score, matched_keywords, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats)
        if success:
            log_posted(source, entry, score, category, level, matched_keywords)
            posts_made += 1
        else:
            skipped += 1
        time.sleep(10)

    summary = f"Attempted to post {len(selected_for_posting)} articles. Successfully posted {posts_made}. Skipped {skipped}."
    if posts_made > 0:
        logger.info(summary)
    else:
        logger.info(summary)

if __name__ == "__main__":
    main()
