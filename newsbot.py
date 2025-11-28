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
import difflib
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME',
    'REDDITPASSWORD'
]
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)
REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
REDDIT_PASSWORD = os.environ['REDDITPASSWORD']
try:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD,
        user_agent='BreakingUKNewsBot/1.0'
    )
    subreddit = reddit.subreddit('BreakingUKNews')
except Exception as e:
    logger.error(f"Failed to initialize Reddit API: {e}")
    sys.exit(1)
DEDUP_FILE = './posted_timestamps.txt'
FUZZY_DUPLICATE_THRESHOLD = 0.40
def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))
def normalize_title(title):
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title
def get_post_title(entry):
    return html.unescape(entry.title).strip()
def get_content_hash(entry):
    content = html.unescape(entry.title + " " + getattr(entry, "summary", "")[:300])
    return hashlib.md5(content.encode('utf-8')).hexdigest()
def load_dedup(filename=DEDUP_FILE):
    urls, titles, hashes = set(), set(), set()
    cleaned_lines = []
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4:
                    try:
                        timestamp = dateparser.parse(parts[0])
                        if timestamp > seven_days_ago:
                            url = parts[1]
                            title = '|'.join(parts[2:-1])
                            hash_ = parts[-1]
                            urls.add(url)
                            titles.add(title)
                            hashes.add(hash_)
                            cleaned_lines.append(line)
                    except (ValueError, ParserError):
                        logger.warning(f"Skipping invalid dedup line: {line.strip()}")
                        continue
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cleaned_lines)
    logger.info(f"Loaded {len(urls)} unique entries from deduplication file (last 7 days)")
    return urls, titles, hashes
posted_urls, posted_titles, posted_hashes = load_dedup()
def is_duplicate(entry):
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    if norm_link in posted_urls:
        return True, "Duplicate URL"
    for pt in posted_titles:
        if difflib.SequenceMatcher(None, pt, norm_title).ratio() > FUZZY_DUPLICATE_THRESHOLD:
            return True, "Duplicate Title (Fuzzy Match)"
    if content_hash in posted_hashes:
        return True, "Duplicate Content Hash"
    return False, ""
def add_to_dedup(entry):
    norm_link = normalize_url(entry.link)
    post_title = get_post_title(entry)
    norm_title = normalize_title(post_title)
    content_hash = get_content_hash(entry)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp}|{norm_link}|{norm_title}|{content_hash}\n")
    posted_urls.add(norm_link)
    posted_titles.add(norm_title)
    posted_hashes.add(content_hash)
    logger.info(f"Added to deduplication: {norm_title}")
def get_entry_published_datetime(entry):
    for field in ['published', 'updated', 'created', 'date']:
        if hasattr(entry, field):
            try:
                dt = dateparser.parse(getattr(entry, field))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, ParserError):
                continue
    return None
def extract_first_paragraphs(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        raw_paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in raw_paragraphs:
            p_lower = p.lower()
            if ('browser' in p_lower and 'use' in p_lower) or 'view in browser' in p_lower or 'open in your browser' in p_lower or re.search(r'open (this|the) (article|page|link)', p_lower):
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', p_lower) or re.search(r'(^|\n)\s*by\s+[A-Z][\w\-\']+', p):
                continue
            if 'copyright' in p_lower or '(c)' in p_lower or '©' in p_lower or 'read our policy' in p_lower or 'external links' in p_lower or 'read more about' in p_lower:
                continue
            filtered.append(p)
            if len(filtered) >= 3:
                break
        while len(filtered) < 3:
            filtered.append("")
        return filtered[:3]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        return ["", "", ""]
def get_full_article_text(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        raw_paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 40]
        filtered = []
        for p in raw_paragraphs:
            p_lower = p.lower()
            if ('browser' in p_lower and 'use' in p_lower) or 'view in browser' in p_lower or 'open in your browser' in p_lower or re.search(r'open (this|the) (article|page|link)', p_lower):
                continue
            if re.search(r'(^|\b)(written by|reported by)\b', p_lower) or re.search(r'(^|\n)\s*by\s+[A-Z][\w\-\']+', p):
                continue
            if 'copyright' in p_lower or '(c)' in p_lower or '©' in p_lower or 'read our policy' in p_lower or 'external links' in p_lower or 'read more about' in p_lower:
                continue
            filtered.append(p)
        return ' '.join(filtered)
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch full text from URL {url}: {e}")
        return ""
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
    "workout", "product", "seasonal", "deals", "us open", "mixed doubles", "tennis tournament",
    "nfl", "nba", "super bowl", "mlb", "nhl", "oscars", "grammy", "best", "tested", "recommended"
]
SPORTS_PREVIEW_KEYWORDS = [
    "boxing match", "fight night", "upcoming fight", "bout", "weigh-in", "fight card",
    "preview", "prediction", "odds", "betting"
]
UK_KEYWORDS = {
    "london": 4, "parliament": 4, "westminster": 4, "downing street": 4, "buckingham palace": 4,
    "nhs": 4, "bank of england": 4, "ofgem": 4, "bbc": 4, "itv": 4, "sky news": 4,
    "manchester": 4, "birmingham": 4, "glasgow": 4, "edinburgh": 4, "cardiff": 4, "belfast": 4,
    "liverpool": 4, "leeds": 4, "bristol": 4, "newcastle": 4, "sheffield": 4, "nottingham": 4,
    "leominster": 4, "herefordshire": 4, "shropshire": 4, "worcestershire": 4, "devon": 4, "cornwall": 4,
    "norfolk": 4, "suffolk": 4, "kent": 4, "sussex": 4, "essex": 4, "yorkshire": 4, "cumbria": 4,
    "premier league": 4, "wimbledon": 4, "glastonbury": 4, "the ashes": 4, "royal ascot": 4,
    "house of commons": 4, "house of lords": 4, "met police": 4, "scotland yard": 4,
    "national trust": 4, "met office": 4, "british museum": 4, "tate modern": 4,
    "level crossing": 4, "west midlands railway": 4, "network rail": 4,
    "ofsted": 4, "dvla": 4, "hmrc": 4, "dwp": 4, "tory": 4, "labour party": 4, "reform uk": 4, "plaid cymru": 4,
    "brighton": 4, "southampton": 4, "plymouth": 4, "hull": 4, "derby": 4,
    "uk": 5, "britain": 5, "united kingdom": 5, "england": 5, "scotland": 5, "wales": 5, "northern ireland": 5,
    "british": 4, "labour": 4, "conservative": 4, "lib dem": 4, "snp": 4, "green party": 4,
    "king charles": 4, "queen camilla": 4, "prince william": 4, "princess kate": 4,
    "keir starmer": 4, "rachel reeves": 4, "kemi badenoch": 4, "ed davey": 4, "john swinney": 4,
    "angela rayner": 4, "nigel farage": 4, "carla denyer": 4, "adrian ramsay": 4,
    "yvette cooper": 4, "david lammy": 4, "pat mcfadden": 4, "shabana mahmood": 4,
    "wes streeting": 4, "john healey": 4,
    "brexit": 4, "pound sterling": 4, "great british": 4, "oxford": 4, "cambridge": 4,
    "village": 2, "county": 2, "borough": 2, "railway": 2,
    "government": 2, "economy": 2, "policy": 2, "election": 2, "inflation": 2, "cost of living": 2,
    "prime minister": 3, "chancellor": 3, "home secretary": 3, "a-levels": 3, "gcse": 3,
    "council tax": 3, "energy price cap": 3, "high street": 3, "pub": 3, "motorway": 3,
    "council": 3, "home office": 3, "raducanu": 4, "councillor": 3, "hospital": 2,
    "morrisons": 4, "co-op": 4, "iceland": 4, "whole foods": 4,
    "sainsbury's": 4, "tesco": 4, "asda": 4, "marks and spencer": 4, "waitrose": 4,
    "borough market": 4, "portobello market": 4, "covent garden": 4,
    "stonehenge": 4, "lake district": 4, "snowdonia": 4, "giant's causeway": 4,
    "hadrian's wall": 4, "edinburgh festival": 4, "notting hill carnival": 4,
    "british airways": 4, "easyjet": 4, "ryanair": 4, "heathrow": 4, "gatwick": 4,
    "london underground": 4, "tube": 4, "national rail": 4,
    "stormont": 4, "senedd": 4, "holyrood": 4,
    "sterling": 3, "british isles": 3, "english channel": 3, "north sea": 3,
    "channel tunnel": 3, "eurostar": 3, "ferry": 3, "dover": 3, "calais": 3,
    "ireland": -1, "republic of ireland": -2
}
NEGATIVE_KEYWORDS = {
    "washington": -3, "washington dc": -3, "houston": -3, "arlington": -3, "charleston": -3, "newton": -3, "clinton": -3, "hampton": -3, "burlington": -3,
    "congress": -3, "senate": -3, "white house": -3, "capitol hill": -3,
    "california": -3, "texas": -3, "new york": -3, "los angeles": -3, "chicago": -3,
    "florida": -3, "boston": -3, "miami": -3, "san francisco": -3, "seattle": -3,
    "fbi": -3, "cia": -3, "pentagon": -3, "supreme court": -3, "biden": -3, "trump": -3,
    "kamala harris": -3, "jd vance": -3,
    "super bowl": -3, "nfl": -3, "nba": -3, "wall street": -3,
    "potus": -3, "scotus": -3, "arizona": -3, "nevada": -3, "georgia": -3,
    "emmanuel macron": -2, "marine le pen": -2, "elysee": -2, "french parliament": -2,
    "olaf scholz": -2, "bundestag": -2,
    "vladimir putin": -2, "kremlin": -2,
    "xi jinping": -2, "ccp": -2,
    "narendra modi": -2, "lok sabha": -2,
    "justin trudeau": -2, "ottawa": -2,
    "anthony albanese": -2, "canberra": -2,
    "france": -2, "germany": -2, "china": -2, "russia": -2, "india": -2,
    "australia": -2, "canada": -2, "japan": -2, "brazil": -2, "south africa": -2,
    "paris": -2, "berlin": -2, "tokyo": -2, "sydney": -2, "toronto": -2,
    "nato": -2, "united nations": -2, "olympics": -2, "world cup": -2,
    "brussels": -2, "rome": -2, "madrid": -2, "beijing": -2, "moscow": -2, "new delhi": -2,
    "us open": -10, "mixed doubles": -5, "tennis tournament": -3,
    "mattress": -5, "back pain": -3, "best mattresses": -10,
    "celebrity": -4, "gossip": -5, "hollywood": -3
}
strong_uk_keywords = [
    "uk", "britain", "united kingdom", "england", "scotland", "wales", "northern ireland",
    "london", "manchester", "birmingham", "glasgow", "edinburgh", "cardiff", "belfast",
    "liverpool", "leeds", "bristol", "newcastle", "sheffield", "nottingham", "brighton",
    "southampton", "plymouth", "hull", "derby", "oxford", "cambridge"
]
def calculate_uk_relevance_score(text, url=""):
    score = 0
    matched_keywords = {}
    text_lower = text.lower()
    negative_keys = set(k.lower() for k in NEGATIVE_KEYWORDS)
    for keyword, weight in UK_KEYWORDS.items():
        count = len(re.findall(r'\b' + re.escape(keyword) + r'\b', text_lower))
        if count > 0:
            score += weight * count
            matched_keywords[keyword] = count
    for keyword, weight in NEGATIVE_KEYWORDS.items():
        count = len(re.findall(r'\b' + re.escape(keyword) + r'\b', text_lower))
        if count > 0:
            score += weight * count
            matched_keywords[f"negative:{keyword}"] = count
    # Single word places
    placenames = re.findall(r'\b(\w+(shire|ford|ton|ham|bridge|cester))\b', text_lower)
    # Multi word places
    placenames += re.findall(r'\b(\w+\s+\w+(shire|ford|ton|ham|bridge|cester))\b', text_lower)
    for pn in placenames:
        pn_lower = pn[0].lower()
        if pn_lower in negative_keys:
            continue
        if pn_lower not in UK_KEYWORDS:
            count = len(re.findall(r'\b' + re.escape(pn_lower) + r'\b', text_lower))
            score += 2 * count
            matched_keywords[pn_lower] = count
    postcodes = re.findall(r'\b([a-z]{1,2}\d{1,2}[a-z]?\s*\d[a-z]{2})\b', text_lower)
    for pc in postcodes:
        pc_upper = pc.upper().replace(' ', '')
        count = len(re.findall(re.escape(pc), text_lower))
        score += 2 * count
        matched_keywords[pc_upper] = count
    return score, matched_keywords
def get_relevance_level(score, matched_keywords):
    has_strong_uk = any(kw in matched_keywords for kw in strong_uk_keywords)
    if score >= 10:
        level = "Very High"
    elif score >= 7 or has_strong_uk:
        level = "High"
    elif score >= 4:
        level = "Medium"
    elif score >= 2:
        level = "Low"
    else:
        level = "Very Low"
    return level
def is_promotional(entry):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    if "offer" in combined:
        if any(kw in combined for kw in ["government", "nhs", "pay", "policy", "public sector"]):
            return False
    return any(kw in combined for kw in PROMOTIONAL_KEYWORDS)
def is_opinion(entry):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in OPINION_KEYWORDS)
def is_irrelevant_fluff(entry):
    combined = html.unescape(entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in combined for kw in IRRELEVANT_KEYWORDS)
def is_sports_preview(text):
    text_lower = text.lower()
    has_preview = any(kw in text_lower for kw in SPORTS_PREVIEW_KEYWORDS)
    result_keywords = ["won", "wins", "winner", "defeated", "beat", "victory", "champion", "result", "defeats", "beats", "crowned", "triumphs", "claims title"]
    has_result = any(kw in text_lower for kw in result_keywords)
    if has_preview and not has_result:
        return True
    return False
CATEGORY_KEYWORDS = {
    "Politics": ["politics", "parliament", "government", "election", "policy", "minister", "mp", "prime minister", "brexit", "eu", "tory", "labour", "bill", "debate", "vote", "opposition", "party", "manifesto", "legislation", "budget", "cabinet"],
    "Immigration": ["immigration", "immigrant", "asylum", "refugee", "migrant", "border control", "visa", "deportation", "home office", "rwanda", "channel crossing", "migration policy"],
    "Trade and Diplomacy": ["trade", "diplomacy", "diplomatic", "ambassador", "summit", "bilateral", "multilateral", "agreement", "pact", "negotiation", "tariff", "export", "import", "foreign secretary", "embassy", "treaty"],
    "Economy": ["economy", "budget", "inflation", "gdp", "recession", "bank of england", "chancellor", "cost of living", "company", "retail", "business", "stores", "closure", "investment", "market", "unemployment", "tax", "sterling"],
    "Crime & Legal": ["crime", "police", "court", "legal", "arrest", "trial", "investigation", "prosecution", "murder", "killing", "death", "stabbed", "shot", "shooting", "assault", "attack", "robbery", "burglary", "theft", "fraud", "drugs", "knife crime", "gun crime", "arrested", "charged", "sentenced", "jailed", "prison", "offender", "victim", "metropolitan police", "suspect", "injured", "conviction", "bail"],
    "Royals": ["royal", "monarchy", "king", "queen", "prince", "princess", "palace", "crown", "royal family", "succession"],
    "Sport": ["sport", "football", "cricket", "tennis", "olympics", "match", "game", "tournament", "rugby", "formula 1", "premier league", "wimbledon", "athletics", "boxing", "mma", "ufc", "wrestling"],
    "Culture": ["culture", "art", "music", "film", "theatre", "festival", "book", "literary", "concert", "album", "movie", "series", "tv show", "drama", "comedy", "museum", "gallery", "glastonbury", "exhibition", "heritage"],
    "National Newspapers Front Pages": ["front page", "headlines", "newspaper", "today's papers", "daily mail", "guardian", "times", "telegraph", "mirror", "sun", "express", "ft", "financial times"],
    "Notable International": ["international", "world", "global", "nasa", "space", "moon", "planet", "earth", "foreign", "un", "internationally", "science", "climate", "global summit"]
}
specific_categories = list(CATEGORY_KEYWORDS.keys()) + ["Notable International"]
priority_order = ["Crime & Legal", "Politics", "Economy", "Immigration", "Trade and Diplomacy", "Royals", "Sport", "Culture", "National Newspapers Front Pages", "Notable International"]
def get_category(full_combined, full_text):
    text = html.unescape(full_text).lower()
    matched_cats = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        cat_matched = {}
        for keyword in keywords:
            count = len(re.findall(r'\b' + re.escape(keyword) + r'\b', text))
            if count > 0:
                cat_matched[keyword] = count
        if cat_matched:
            matched_cats[cat] = cat_matched
    uk_score, uk_matched_keywords = calculate_uk_relevance_score(full_combined)
    if not matched_cats:
        return "Notable International", {}, {}, [], {}, uk_score, uk_matched_keywords
    cat_scores = {cat: sum(matched.values()) for cat, matched in matched_cats.items()}
    max_score = max(cat_scores.values())
    candidates = [cat for cat, score in cat_scores.items() if score == max_score]
    chosen_cat = min(candidates, key=lambda c: priority_order.index(c) if c in priority_order else len(priority_order))
    has_foreign = any('negative:' in k for k in uk_matched_keywords)
    has_strong_uk = any(k in uk_matched_keywords for k in strong_uk_keywords)
    if chosen_cat == "Politics" and has_foreign and not has_strong_uk:
        chosen_cat = "Notable International"
    if chosen_cat == "Crime & Legal" and any(si in full_combined for si in ["boxing", "mma", "ufc", "wrestling", "fight", "bout"]) and not any(ci in full_combined for ci in ["police", "arrest", "charged", "court", "trial", "prosecution", "suspect"]):
        chosen_cat = "Sport"
    cat_keywords = matched_cats.get(chosen_cat, {})
    all_matched_keywords = {kw: count for matched in matched_cats.values() for kw, count in matched.items()}
    all_matched_cats = list(matched_cats.keys())
    return chosen_cat, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, uk_score, uk_matched_keywords
FLAIR_MAPPING = {
    "Politics": "Politics",
    "Culture": "Culture",
    "Sport": "Sport",
    "Crime & Legal": "Crime & Legal",
    "Royals": "Royals",
    "Immigration": "Immigration",
    "Economy": "Economy",
    "Notable International": "Notable International News🌍",
    "National Newspapers Front Pages": "National Newspapers Front Pages",
    "Trade and Diplomacy": "Trade and Diplomacy"
}
DEFAULT_UK_THRESHOLD = 3
CATEGORY_THRESHOLDS = {
    "Sport": 8,
    "Royals": 6,
    "Notable International": 4,
    "Economy": 2
}
def post_to_reddit(entry, score, matched_keywords, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, retries=3, base_delay=10):
    flair_text = FLAIR_MAPPING.get(category, "Notable International News🌍")
    flair_id = None
    try:
        for flair in subreddit.flair.link_templates:
            if flair['text'] == flair_text:
                flair_id = flair['id']
                break
    except Exception as e:
        logger.error(f"Failed to fetch flairs: {e}")
    cat_scores = {cat: sum(matched_cats.get(cat, {}).values()) for cat in all_matched_cats}
    total_score = sum(cat_scores.values()) or 1
    chosen_score = cat_scores.get(category, 0)
    confidence = int(100 * chosen_score / total_score) if total_score > 0 else 50
    for attempt in range(retries):
        try:
            post_title = get_post_title(entry)
            submission = subreddit.submit(
                title=post_title,
                url=entry.link,
                flair_id=flair_id
            )
            logger.info(f"Posted: {submission.shortlink}")
            reply_lines = []
            for para in paragraphs:
                if para:
                    reply_lines.append("> " + para[:200])
                    reply_lines.append("")
            reply_lines.append(f"[Read more]({entry.link})")
            reply_lines.append("")
            reply_lines.append("**UK Relevance**")
            sorted_uk_keywords = sorted(
                [(kw, count) for kw, count in matched_keywords.items() if not kw.startswith("negative:")],
                key=lambda x: -x[1]
            )[:3]
            if sorted_uk_keywords:
                kw_parts = []
                for kw, count in sorted_uk_keywords:
                    times_str = "time" if count == 1 else "times"
                    kw_parts.append(f"{kw.upper()} ({count} {times_str})")
                if len(kw_parts) > 1:
                    formatted_keywords = ", ".join(kw_parts[:-1]) + " and " + kw_parts[-1]
                else:
                    formatted_keywords = kw_parts[0]
                reply_lines.append(f"This article was posted because the system detected key UK-related terms such as {formatted_keywords}, which indicate that it fits the {flair_text} category and is likely of interest to a UK audience.")
            reply_lines.append(f"Based on this assessment, the system automatically assigned the {flair_text} flair with {confidence}% confidence.")
            reply_lines.append("This was posted automatically.")
            reply_lines.append("(For more information about how this works, please see the [subreddit wiki](https://www.reddit.com/r/BreakingUKNews/wiki/index/))")
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
    TARGET_POSTS_PER_RUN = 7
    INITIAL_ARTICLES = 10
    feed_sources = {
        "BBC UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    }
    all_entries = []
    now = datetime.now(timezone.utc)
    six_hours_ago = now - timedelta(hours=6)
    for name, url in feed_sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published_dt = get_entry_published_datetime(entry)
                if published_dt and six_hours_ago <= published_dt <= now + timedelta(minutes=5):
                    all_entries.append((name, entry, published_dt))
        except Exception as e:
            logger.error(f"Error loading feed {name}: {e}")
    all_entries.sort(key=lambda x: x[2], reverse=True)
    all_articles = []
    category_counts = {cat: 0 for cat in specific_categories}
    winner_keywords = ["wins", "defeats", "beats", "victory", "champion", "winner", "crowned", "triumphs", "claims title"]
    for name, entry, published_dt in all_entries:
        if len(all_articles) >= INITIAL_ARTICLES:
            break
        is_dup, reason = is_duplicate(entry)
        if is_dup:
            logger.info(f"Skipped duplicate article ({reason}): {get_post_title(entry)}")
            continue
        if is_promotional(entry):
            logger.info(f"Skipped promotional article: {get_post_title(entry)}")
            continue
        if is_opinion(entry):
            logger.info(f"Skipped opinion article: {get_post_title(entry)}")
            continue
        if is_irrelevant_fluff(entry):
            logger.info(f"Skipped irrelevant fluff article: {get_post_title(entry)}")
            continue
        full_text = get_full_article_text(entry.link)
        if not full_text:
            logger.info(f"Article rejected: {get_post_title(entry)} (Reason: Failed to fetch full text)")
            continue
        full_combined = html.unescape(entry.title + " " + getattr(entry, "summary", "") + " " + full_text).lower()
        category, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats, score, matched_keywords = get_category(full_combined, full_text)
        if category == "Sport":
            if is_sports_preview(full_combined) or not any(kw in full_combined for kw in winner_keywords):
                logger.info(f"Skipped sports article not about winner or preview: {get_post_title(entry)}")
                continue
        logger.info(f"Article: {get_post_title(entry)} | Relevance Score: {score} | Matched: {len(matched_keywords)} keys | Category: {category}")
        has_uk_term = any(not k.startswith("negative:") for k in matched_keywords)
        threshold = CATEGORY_THRESHOLDS.get(category, DEFAULT_UK_THRESHOLD)
        if score < threshold or not has_uk_term:
            logger.info(f"Article rejected: {get_post_title(entry)} (Reason: Score {score} below threshold {threshold} or no UK terms)")
            continue
        level = get_relevance_level(score, matched_keywords)
        if level in ["Low", "Very Low"]:
            logger.info(f"Article rejected: {get_post_title(entry)} (Reason: Low relevance level {level})")
            continue
        paragraphs = extract_first_paragraphs(entry.link)
        norm_title = normalize_title(get_post_title(entry))
        if category_counts.get(category, 0) < 3:
            logger.info(f"Selected article: {get_post_title(entry)} | Score: {score} | Category: {category}")
            all_articles.append((name, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats))
            category_counts[category] = category_counts.get(category, 0) + 1
    unique_articles = []
    seen_urls = set()
    seen_titles = set()
    seen_hashes = set()
    for article in all_articles:
        source, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats = article
        norm_link = normalize_url(entry.link)
        content_hash = get_content_hash(entry)
        is_dup = norm_link in seen_urls
        if not is_dup:
            for st in seen_titles:
                if difflib.SequenceMatcher(None, st, norm_title).ratio() > FUZZY_DUPLICATE_THRESHOLD:
                    is_dup = True
                    break
        if not is_dup and content_hash in seen_hashes:
            is_dup = True
        if not is_dup:
            unique_articles.append(article)
            seen_urls.add(norm_link)
            seen_titles.add(norm_title)
            seen_hashes.add(content_hash)
    all_articles = unique_articles
    all_articles.sort(key=lambda x: x[2], reverse=True)
    selected_for_posting = []
    temp_category_counts = {cat: 0 for cat in specific_categories}
    for article in all_articles:
        if len(selected_for_posting) >= TARGET_POSTS_PER_RUN:
            break
        source, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats = article
        if temp_category_counts.get(category, 0) < 3:
            selected_for_posting.append(article)
            temp_category_counts[category] += 1
    posts_made = 0
    skipped = 0
    for article in selected_for_posting:
        source, entry, score, matched_keywords, norm_title, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats = article
        success = post_to_reddit(entry, score, matched_keywords, category, paragraphs, cat_keywords, all_matched_keywords, all_matched_cats, matched_cats)
        if success:
            logger.info(f"Successfully posted from {source}: {get_post_title(entry)}")
            posts_made += 1
        else:
            logger.error(f"Failed to post an article from {source}: {get_post_title(entry)}")
            skipped += 1
        time.sleep(10)
    logger.info(f"Attempted to post {len(selected_for_posting)} articles. Successfully posted {posts_made}. Skipped {skipped}.")
if __name__ == "__main__":
    main()
