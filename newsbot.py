from __future__ import annotations

import base64
import difflib
import getpass
import hashlib
import html
import json
import os
import random
import re
import string
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone

feedparser    = None
requests      = None
BeautifulSoup = None
dateparser    = None


class Col:
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    CYAN    = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE   = '\033[97m'
    DIM     = '\033[2m'
    RESET   = '\033[0m'


def log(tag, msg, color=Col.RESET):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{color}[{ts}] [{tag}] {msg}{Col.RESET}", flush=True)


def log_score_detail(entry_title, score, pos, neg, matched, decision, reason):
    pos_hits = {k: v for k, v in matched.items() if not k.startswith("NEG:")}
    neg_hits = {k[4:]: v for k, v in matched.items() if k.startswith("NEG:")}
    log("SCORE", f"{'─' * 60}", Col.DIM)
    log("SCORE", f"{Col.WHITE}{entry_title[:70]}{Col.RESET}", Col.DIM)
    log("SCORE", f"Total={Col.YELLOW}{score:+d}{Col.RESET}  "
                 f"UK={Col.GREEN}+{pos}{Col.RESET}  "
                 f"Non-UK={Col.RED}-{neg}{Col.RESET}  "
                 f"→ {Col.CYAN}{decision}{Col.RESET}", Col.DIM)
    if pos_hits:
        kw = ", ".join(f"{Col.GREEN}{k}{Col.RESET}(×{v})"
                       for k, v in sorted(pos_hits.items(), key=lambda x: -x[1])[:8])
        log("SCORE", f"UK hits: {kw}", Col.DIM)
    if neg_hits:
        kw = ", ".join(f"{Col.RED}{k}{Col.RESET}(×{v})"
                       for k, v in sorted(neg_hits.items(), key=lambda x: -x[1])[:4])
        log("SCORE", f"Non-UK:  {kw}", Col.DIM)
    log("SCORE", f"Reason: {Col.MAGENTA}{reason}{Col.RESET}", Col.DIM)
    log("SCORE", f"{'─' * 60}", Col.DIM)


def log_reasoning_block(prefix, title, decision, reasoning, extra=None):
    log("REASONING", f"{'═' * 70}", Col.MAGENTA)
    log("REASONING", f"  {prefix}: {Col.WHITE}{title[:80]}{Col.RESET}", Col.MAGENTA)
    log("REASONING", f"  Decision: {Col.CYAN}{decision}{Col.RESET}", Col.MAGENTA)
    line = "  Reasoning: "
    for word in (reasoning or "(no reasoning)").split():
        if len(line) + len(word) + 1 > 78:
            log("REASONING", line, Col.MAGENTA)
            line = "             " + word
        else:
            line = (line + " " + word).strip() if line.endswith(":") else line + " " + word
    if line.strip():
        log("REASONING", line, Col.MAGENTA)
    if extra:
        for k, v in extra.items():
            log("REASONING", f"  {k}: {v}", Col.MAGENTA)
    log("REASONING", f"{'═' * 70}", Col.MAGENTA)


def clean_text(s):
    if not s:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = html.unescape(s)
    s = html.unescape(s)
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


REASONING_LOG_FILE = "ai_reasoning_log.jsonl.enc"
_S = b"newsbot-reasoning-v1"
_I = 480_000


def _derive_fernet_key(passcode):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_S, iterations=_I)
    return base64.urlsafe_b64encode(kdf.derive(passcode.encode("utf-8")))


def _init_fernet_from_env():
    passcode = os.environ.get("REASONING_PASSCODE", "").strip()
    if not passcode:
        return None, "REASONING_PASSCODE not set"
    try:
        from cryptography.fernet import Fernet
        return Fernet(_derive_fernet_key(passcode)), None
    except ImportError:
        return None, "`cryptography` package not installed"
    except Exception as e:
        return None, f"init error: {e}"


_FERNET = None


def append_encrypted_reasoning(record):
    if _FERNET is None:
        return
    try:
        plaintext = json.dumps(record, ensure_ascii=False).encode("utf-8")
        token = _FERNET.encrypt(plaintext).decode("utf-8")
        with open(REASONING_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(token + "\n")
    except Exception as e:
        log("REASONING", f"append failed: {e}", Col.YELLOW)


DEDUP_FILE    = "posted_urls.txt"
AI_CACHE_FILE = "ai_cache.json"
METRICS_FILE  = "metrics.json"

IN_RUN_FUZZY_THRESHOLD  = 0.55
TARGET_POSTS            = 8
MAX_PER_SOURCE          = 3
INITIAL_ARTICLES        = 80
TIME_WINDOW_HOURS       = 12
MAX_KEYWORD_REPEATS     = 3
DISTINCT_UK_KW_REQUIRED = 2

GROQ_MODEL   = "llama-3.1-8b-instant"
GROQ_RPM     = 25
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_RPM   = 12

AI_MAX_RETRIES = 2
AI_BASE_DELAY  = 2.0
AI_MAX_DELAY   = 20.0

REQUEST_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/124.0.0.0 Safari/537.36'),
    'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
               'image/avif,image/webp,image/apng,*/*;q=0.8'),
    'Accept-Language': 'en-GB,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate',
    'Referer': 'https://www.google.com/',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}


UK_KEYWORDS = {
    "uk": 6, "united kingdom": 6, "britain": 6, "great britain": 6,
    "nhs": 6, "national health service": 6,
    "england": 5, "scotland": 5, "wales": 5, "northern ireland": 5,
    "london": 5, "westminster": 5, "parliament": 5, "downing street": 5,
    "house of commons": 5, "house of lords": 5, "prime minister": 5,
    "holyrood": 5, "stormont": 5, "senedd": 5, "devolution": 5,
    "king charles": 5, "prince william": 5, "princess of wales": 5,
    "home office": 4, "foreign office": 4, "foreign commonwealth": 4,
    "treasury": 4, "bank of england": 4, "chancellor": 4,
    "met police": 4, "metropolitan police": 4, "scotland yard": 4,
    "hmrc": 4, "companies house": 4, "ofcom": 4, "ofsted": 4, "ofgem": 4,
    "environment agency": 4, "electoral commission": 4,
    "financial conduct authority": 4, "fca": 4, "serious fraud office": 4,
    "uk health security agency": 4, "ukhsa": 4, "mhra": 4,
    "office for national statistics": 4, "ons": 4,
    "office for budget responsibility": 4, "obr": 4,
    "care quality commission": 4, "cqc": 4,
    "ministry of defence": 4, "mod": 4,
    "gchq": 4, "mi5": 4, "mi6": 4, "secret intelligence service": 4,
    "dvla": 4, "dvsa": 4,
    "network rail": 4, "hs2": 4, "transport for london": 4, "tfl": 4,
    "met office": 4, "national grid": 4,
    "bbc news": 4, "sky news": 4,
    "ftse": 4, "ftse 100": 4, "ftse 250": 4,
    "cbi": 4, "tuc": 4,
    "labour party": 4, "conservative party": 4, "tory": 4, "tories": 4,
    "lib dem": 4, "liberal democrat": 4, "liberal democrats": 4,
    "snp": 4, "scottish national party": 4, "reform uk": 4,
    "plaid cymru": 4, "dup": 4, "sinn fein": 4, "alliance party": 4,
    "green party": 3,
    "keir starmer": 4, "rachel reeves": 4, "yvette cooper": 4,
    "angela rayner": 4, "david lammy": 4, "wes streeting": 4,
    "pat mcfadden": 4, "bridget phillipson": 4, "ed miliband": 4,
    "nigel farage": 4, "kemi badenoch": 4, "ed davey": 4, "john swinney": 4,
    "manchester": 4, "birmingham": 4, "leeds": 4, "glasgow": 4,
    "edinburgh": 4, "cardiff": 4, "belfast": 4, "liverpool": 4,
    "sheffield": 4, "bristol": 4, "newcastle": 4, "nottingham": 4,
    "old bailey": 4, "crown court": 4, "supreme court": 4,
    "court of appeal": 4, "high court": 4, "magistrates court": 4,
    "judicial review": 4, "coroner": 4, "inquest": 4,
    "gilt": 4, "gilts": 4, "sterling": 4, "pound sterling": 4,
    "base rate": 4, "monetary policy committee": 4, "mpc": 4,
    "autumn statement": 4, "spring statement": 4, "spending review": 4,
    "universal credit": 4, "personal independence payment": 4,
    "council tax": 4, "stamp duty": 4, "national insurance": 4,
    "cost of living": 4,
    "nhs england": 4, "nhs scotland": 4, "nhs wales": 4,
    "nice": 4, "accident and emergency": 4,
    "ambulance trust": 4, "integrated care": 4, "icb": 4,
    "heathrow": 4, "gatwick": 4, "stansted": 4, "luton airport": 4,
    "national rail": 4, "eurostar": 4, "royal mail": 4, "post office": 4,
    "rolls-royce": 4, "bae systems": 4, "bp": 4, "shell uk": 4,
    "barclays": 4, "lloyds": 4, "natwest": 4, "hsbc uk": 4,
    "tesco": 4, "sainsbury": 4, "asda": 4, "marks and spencer": 4,
    "oxford": 3, "cambridge": 3, "york": 3, "aberdeen": 3, "dundee": 3,
    "swansea": 3, "newport": 3, "derby": 3, "coventry": 3, "hull": 3,
    "english channel": 3, "north sea": 3, "irish sea": 3,
    "the midlands": 3, "east anglia": 3, "cornwall": 3, "yorkshire": 3,
    "russell group": 3, "ucl": 3, "imperial college": 3, "lse": 3,
    "nhs trust": 3, "mental health trust": 3, "british": 3,
    "english": 2, "scottish": 2, "welsh": 2,
    "ulster": 2, "whitehall": 2, "cabinet": 2, "backbench": 2,
    "mp": 2, "msp": 2, "assembly member": 2,
    "home secretary": 2, "foreign secretary": 2, "health secretary": 2,
    "education secretary": 2, "defence secretary": 2,
    "shadow chancellor": 2, "shadow home secretary": 2,
    "welsh government": 2, "scottish government": 2,
    "northern ireland executive": 2,
    "armed forces": 2, "special air service": 2, "sas": 2,
    "help to buy": 2, "british passport": 2, "right to remain": 2,
    "a&e": 2, "gp surgery": 2, "pip": 2,
}

NEGATIVE_KEYWORDS = {
    "biden": -12, "trump": -12, "harris": -10, "maga": -10,
    "republican party": -8, "democratic party": -8,
    "white house": -8, "oval office": -8, "congress": -8, "senate": -8,
    "house of representatives": -8, "capitol hill": -8,
    "fbi": -6, "cia": -6, "pentagon": -6, "federal reserve": -6,
    "wall street": -6, "nasdaq": -5, "dow jones": -5, "sec": -5,
    "fda": -6, "cdc": -6,
    "washington": -6, "new york city": -5, "los angeles": -5,
    "california": -5, "texas": -5, "florida": -5,
    "putin": -8, "kremlin": -8, "xi jinping": -8,
    "chinese communist party": -6, "netanyahu": -5, "narendra modi": -6,
    "anthony albanese": -6, "justin trudeau": -6, "macron": -3,
    "nfl": -6, "nba": -6, "mlb": -6, "nhl": -5, "super bowl": -8,
    "fox news": -6, "cnn": -4, "msnbc": -5, "new york times": -4,
}

BANNED_PHRASES = [
    "not coming to the uk", "isn't coming to the uk", "won't be available in the uk",
    "i tried the", "review:", "hands-on with", "best smartphone", "where to watch",
    "fantasy football", "fpl", "opinion:", "comment:", "letters:", "wordle", "crossword"
]

FLUFF_PATTERNS = [
    re.compile(r"^Why\s", re.I),
    re.compile(r"^How\s", re.I),
    re.compile(r"^Here'?s\s", re.I),
    re.compile(r"^\d+\s(ways|things|reasons)", re.I)
]

FLAIR_CACHE = {}


def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]


UK_PATTERNS  = compile_keywords_dict(UK_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)


class NewsEntry:
    def __init__(self, source, title, link, summary, published, entry_obj=None):
        self.source    = source
        self.title     = clean_text(title)
        self.link      = link
        self.summary   = clean_text(summary)
        self.published = published
        self.entry_obj = entry_obj


def normalize_url(u):
    if not u:
        return ""
    p = urllib.parse.urlparse(u)
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), '', '', ''))


def normalize_title(t):
    if not t:
        return ""
    t = clean_text(t)
    t = re.sub(r"[^\w\s£$€]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def content_hash(text_blob):
    return hashlib.md5(text_blob.encode('utf-8')).hexdigest()


def generate_ref():
    letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    digits  = ''.join(random.choices(string.digits, k=4))
    return f"{letters}-{digits}"


def load_json_data(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return default_val


def save_json_data(filepath, data):
    try:
        tmp = filepath + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, filepath)
    except Exception:
        pass


def update_metrics(source, category):
    data = load_json_data(METRICS_FILE, {"sources": {}, "categories": {}})
    data["sources"][source]      = data["sources"].get(source, 0) + 1
    data["categories"][category] = data["categories"].get(category, 0) + 1
    save_json_data(METRICS_FILE, data)


def load_dedup():
    urls, titles, hashes = set(), set(), set()
    cleaned_lines = []
    parse_errors = 0
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    if not os.path.exists(DEDUP_FILE):
        return urls, titles, hashes
    try:
        with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) < 4:
                    parse_errors += 1
                    cleaned_lines.append(line + '\n')
                    continue
                try:
                    ts = dateparser.parse(parts[0])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts > seven_days_ago:
                        urls.add(parts[1])
                        titles.add(parts[2])
                        hashes.add(parts[-1])
                        cleaned_lines.append(line + '\n')
                except Exception:
                    parse_errors += 1
                    cleaned_lines.append(line + '\n')
    except Exception as e:
        log("DEDUP", f"read failed, leaving file untouched: {e}", Col.YELLOW)
        return urls, titles, hashes

    try:
        tmp = DEDUP_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(cleaned_lines)
        os.replace(tmp, DEDUP_FILE)
    except Exception as e:
        log("DEDUP", f"rewrite failed: {e}", Col.YELLOW)

    log("DEDUP", f"loaded {len(urls)} urls, {len(titles)} titles, "
                 f"{len(hashes)} hashes (parse errors: {parse_errors})", Col.DIM)
    return urls, titles, hashes


POSTED_URLS, POSTED_TITLES, POSTED_HASHES = set(), set(), set()


def add_to_dedup(entry_obj, title_override=None, url_override=None):
    ts = datetime.now(timezone.utc).isoformat()
    if hasattr(entry_obj, 'link'):
        link, title, summary = entry_obj.link, entry_obj.title, getattr(entry_obj, 'summary', '')
    else:
        link, title, summary = url_override, title_override, ""
    norm_link  = normalize_url(link)
    norm_title = normalize_title(title)
    h = content_hash(title + summary)
    try:
        with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{ts}|{norm_link}|{norm_title}|{h}\n")
    except Exception:
        pass
    POSTED_URLS.add(norm_link)
    POSTED_TITLES.add(norm_title)
    POSTED_HASHES.add(h)


def extract_jsonld_paragraphs(soup):
    bodies = []
    for tag in soup.find_all('script', type='application/ld+json'):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                graph = node.get('@graph')
                if isinstance(graph, list):
                    stack.extend(graph)
                body = node.get('articleBody')
                if isinstance(body, str) and len(body.strip()) > 80:
                    bodies.append(body.strip())
    if not bodies:
        return []
    body = max(bodies, key=len)
    parts = [s.strip() for s in re.split(r'\n+', body) if len(s.strip()) > 40]
    if len(parts) < 2:
        sentences = re.split(r'(?<=[.!?])\s+', body)
        parts, buf = [], ''
        for s in sentences:
            buf = (buf + ' ' + s).strip()
            if len(buf) > 220:
                parts.append(buf)
                buf = ''
        if buf and len(buf) > 40:
            parts.append(buf)
    return [clean_text(p) for p in parts]


def extract_paragraphs(soup):
    paras = extract_jsonld_paragraphs(soup)
    if paras:
        return paras
    for selector in ('article', 'main'):
        container = soup.find(selector)
        if container:
            scoped = [clean_text(p.get_text(" ", strip=True))
                      for p in container.find_all('p')
                      if len(p.get_text(strip=True)) > 40]
            if scoped:
                return scoped
    return [clean_text(p.get_text(" ", strip=True)) for p in soup.find_all('p')
            if len(p.get_text(strip=True)) > 40]


def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=15, headers=REQUEST_HEADERS, allow_redirects=True)
        if r.status_code != 200:
            log("FETCH", f"HTTP {r.status_code} for {url[:60]}", Col.YELLOW)
            return []
        soup = BeautifulSoup(r.content, 'html.parser')
        return extract_paragraphs(soup)
    except Exception as e:
        log("FETCH", f"Error fetching {url[:60]}: {e}", Col.YELLOW)
        return []


def calculate_score(text):
    text_l = text.lower()
    score, pos, neg, matched = 0, 0, 0, {}
    for k, w, pat in UK_PATTERNS:
        raw_count = len(pat.findall(text_l))
        count = min(raw_count, MAX_KEYWORD_REPEATS)
        if count:
            score += w * count
            pos   += w * count
            matched[k] = count
    for k, w, pat in NEG_PATTERNS:
        raw_count = len(pat.findall(text_l))
        count = min(raw_count, MAX_KEYWORD_REPEATS)
        if count:
            score += w * count
            neg   += abs(w) * count
            matched[f"NEG:{k}"] = count
    return score, pos, neg, matched


def is_hard_reject(text, pos, neg):
    t_l = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in t_l:
            return True, f"banned: {phrase}"
    for pat in FLUFF_PATTERNS:
        if pat.search(text):
            return True, "fluff/opinion"
    if neg > max(10, 2.0 * pos):
        return True, "negative dominance"
    return False, ""


def detect_category(text):
    t_l = text.lower()
    cats = {
        "Politics":      ["parliament", "government", "minister", "mp", "election", "brexit", "labour", "tory"],
        "Economy":       ["economy", "inflation", "budget", "tax", "bank"],
        "Crime & Legal": ["police", "court", "trial", "arrest", "murder", "prison"],
        "Sport":         ["football", "cricket", "match", "cup", "trophy"],
        "Royals":        ["royal", "king", "queen", "palace"],
        "Environment":   ["storm", "weather", "flood", "climate", "met office"],
    }
    scores = {c: sum(1 for k in v if k in t_l) for c, v in cats.items()}
    if all(v == 0 for v in scores.values()):
        return "General", 0.0
    return max(scores, key=scores.get), 1.0


def get_flair_id(sub, text):
    key = f"{sub.display_name}:{text}"
    if key in FLAIR_CACHE:
        return FLAIR_CACHE[key]
    try:
        for t in sub.flair.link_templates:
            if t['text'] == text:
                FLAIR_CACHE[key] = t['id']
                return t['id']
    except Exception:
        pass
    return None


class RateLimitedError(Exception):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class ProviderServerError(Exception):
    pass


class ProviderClientError(Exception):
    pass


class Pacer:
    def __init__(self, rpm, buffer_sec=0.25):
        self.min_interval = (60.0 / rpm + buffer_sec) if rpm > 0 else 0.0
        self.last_call_ts = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        now = time.time()
        elapsed = now - self.last_call_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call_ts = time.time()


class AIProvider:
    name = "abstract"

    def __init__(self, api_key, model, rpm_limit):
        self.api_key   = (api_key or "").strip()
        self.model     = model
        self.pacer     = Pacer(rpm_limit)
        self.enabled   = bool(self.api_key)
        self.exhausted = False

    def call(self, prompt):
        if not self.enabled:
            raise ProviderClientError(f"{self.name}: no API key configured")
        if self.exhausted:
            raise ProviderClientError(f"{self.name}: daily quota exhausted earlier")
        self.pacer.wait()
        return self._do_call(prompt)

    def _do_call(self, prompt):
        raise NotImplementedError


class GroqProvider(AIProvider):
    name = "Groq"
    URL  = "https://api.groq.com/openai/v1/chat/completions"

    def _do_call(self, prompt):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
        }
        try:
            r = requests.post(self.URL, headers=headers, json=payload, timeout=30)
        except requests.RequestException as e:
            raise ProviderServerError(f"Groq network: {e}")

        if r.status_code == 429:
            retry_after = None
            ra = r.headers.get("Retry-After") or r.headers.get("retry-after")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    pass
            body_lower = r.text.lower()
            if "rpd" in body_lower or "daily" in body_lower or "requests per day" in body_lower:
                self.exhausted = True
            raise RateLimitedError(f"Groq 429: {r.text[:200]}", retry_after=retry_after)

        if r.status_code >= 500:
            raise ProviderServerError(f"Groq {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise ProviderClientError(f"Groq {r.status_code}: {r.text[:200]}")

        try:
            data = r.json()
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, ValueError, TypeError) as e:
            raise ProviderServerError(f"Groq response parse: {e}")


class GeminiProvider(AIProvider):
    name = "Gemini"

    def __init__(self, api_key, model, rpm_limit):
        super().__init__(api_key, model, rpm_limit)
        self.client = None
        if self.enabled:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
            except ImportError:
                log("AI", "google-genai not installed; Gemini disabled", Col.YELLOW)
                self.enabled = False
            except Exception as e:
                log("AI", f"Gemini init failed: {e}; disabled", Col.YELLOW)
                self.enabled = False

    def _do_call(self, prompt):
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=prompt
            )
            return response.text or ""
        except Exception as e:
            msg = str(e)
            lower = msg.lower()
            if ("429" in msg or "resource_exhausted" in lower
                    or "quota" in lower or "rate" in lower):
                m = re.search(r'retry.{0,15}?(\d+(?:\.\d+)?)', lower)
                retry_after = float(m.group(1)) if m else None
                if "per day" in lower or "rpd" in lower or "daily" in lower:
                    self.exhausted = True
                raise RateLimitedError(f"Gemini rate-limited: {msg[:200]}",
                                       retry_after=retry_after)
            if any(t in msg for t in ("500", "502", "503", "504")) or "unavailable" in lower:
                raise ProviderServerError(f"Gemini server: {msg[:200]}")
            raise ProviderClientError(f"Gemini error: {msg[:200]}")


def call_ai_with_fallback(prompt, providers):
    for provider in providers:
        if not provider.enabled:
            log("AI", f"  ↳ {provider.name}: not configured, skipping", Col.DIM)
            continue
        if provider.exhausted:
            log("AI", f"  ↳ {provider.name}: exhausted this run, skipping", Col.DIM)
            continue

        for attempt in range(AI_MAX_RETRIES + 1):
            try:
                raw = provider.call(prompt)
                if attempt > 0:
                    log("AI", f"  ↳ {provider.name}: succeeded on attempt {attempt + 1}", Col.GREEN)
                return raw, provider.name

            except RateLimitedError as e:
                if attempt >= AI_MAX_RETRIES or provider.exhausted:
                    log("AI", f"  ↳ {provider.name}: rate-limited, giving up", Col.YELLOW)
                    break
                delay = min(AI_BASE_DELAY * (2 ** attempt), AI_MAX_DELAY)
                if e.retry_after:
                    delay = max(delay, float(e.retry_after))
                delay *= random.uniform(0.7, 1.4)
                log("AI", f"  ↳ {provider.name}: 429; sleep {delay:.1f}s "
                          f"(attempt {attempt + 1}/{AI_MAX_RETRIES + 1})", Col.YELLOW)
                time.sleep(delay)

            except ProviderServerError as e:
                if attempt >= AI_MAX_RETRIES:
                    log("AI", f"  ↳ {provider.name}: server error, giving up: {e}", Col.YELLOW)
                    break
                delay = min(AI_BASE_DELAY * (2 ** attempt), AI_MAX_DELAY) * random.uniform(0.7, 1.4)
                log("AI", f"  ↳ {provider.name}: server error; sleep {delay:.1f}s: {e}", Col.YELLOW)
                time.sleep(delay)

            except ProviderClientError as e:
                log("AI", f"  ↳ {provider.name}: client error, falling through: {e}", Col.RED)
                break

    log("AI", "  ↳ All providers exhausted/failed", Col.RED)
    return None, None


AI_PROVIDERS = []


def _parse_ai_json(raw):
    if not raw:
        return False, "(empty AI response)"
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if not cleaned.startswith("{"):
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(0)
    try:
        parsed = json.loads(cleaned)
        decision  = str(parsed.get("decision", "")).strip().upper()
        reasoning = str(parsed.get("reasoning", "")).strip()
        is_rel    = decision == "YES"
        if not reasoning:
            reasoning = "(model returned no reasoning text)"
        return is_rel, reasoning
    except json.JSONDecodeError:
        first_chunk = raw[:60].lower()
        is_rel = "yes" in first_chunk and "no" not in first_chunk.split("yes")[0]
        return is_rel, f"(unparseable JSON; raw response: {raw[:200]})"


def check_ai_relevance(title, summary, excerpt, entry_hash, source="", url=""):
    cache = load_json_data(AI_CACHE_FILE, {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    if entry_hash in cache and cache[entry_hash].get('timestamp', 0) > cutoff:
        is_rel    = cache[entry_hash]['is_relevant']
        reasoning = cache[entry_hash].get('reasoning', '(legacy cache entry)')
        prov      = cache[entry_hash].get('provider', 'cache')
        log("AI", f"Cache hit ({prov}) for {entry_hash[:8]}… → "
                  f"{'YES' if is_rel else 'NO'}", Col.DIM)
        log_reasoning_block(
            prefix=f"AI cached [{prov}]",
            title=title,
            decision="YES (UK relevant)" if is_rel else "NO (not UK)",
            reasoning=reasoning,
        )
        return is_rel, reasoning, prov

    prompt = (
        "You are a strict UK news relevance filter for an automated subreddit. "
        "Determine if this article is hard news genuinely relevant to a UK audience "
        "(UK politics, economy, NHS, crime & legal, devolved governments, royals, "
        "security, major UK infrastructure). Reject fluff, lifestyle, product reviews, "
        "sports previews, opinion columns, and stories where the UK is only mentioned "
        "in passing.\n\n"
        "Respond ONLY with a single-line JSON object, no markdown fences, in this "
        "exact shape:\n"
        '{"decision": "YES" or "NO", "reasoning": "1-3 sentence explanation"}\n\n'
        f"Title: {title}\n"
        f"Summary: {summary}\n"
        f"Excerpt: {excerpt}"
    )

    log("AI", f"Querying AI providers for: {title[:60]}…", Col.CYAN)
    raw, provider_name = call_ai_with_fallback(prompt, AI_PROVIDERS)

    if raw is None:
        reasoning = "(all AI providers unavailable — score-only decision)"
        log("AI", reasoning, Col.RED)
        append_encrypted_reasoning({
            "ts":        datetime.now(timezone.utc).isoformat(),
            "type":      "ai_failure",
            "source":    source,
            "title":     title,
            "url":       url,
            "reasoning": reasoning,
        })
        return None, reasoning, "none"

    is_rel, reasoning = _parse_ai_json(raw)

    log("AI", f"[{provider_name}] Decision: {'YES' if is_rel else 'NO'} — {title[:50]}",
        Col.MAGENTA)
    log_reasoning_block(
        prefix=f"AI fresh [{provider_name}]",
        title=title,
        decision="YES (UK relevant)" if is_rel else "NO (not UK)",
        reasoning=reasoning,
    )

    cache[entry_hash] = {
        "is_relevant": is_rel,
        "reasoning":   reasoning,
        "provider":    provider_name,
        "timestamp":   datetime.now(timezone.utc).timestamp(),
    }
    save_json_data(AI_CACHE_FILE, cache)

    append_encrypted_reasoning({
        "ts":            datetime.now(timezone.utc).isoformat(),
        "type":          "ai_decision",
        "provider":      provider_name,
        "source":        source,
        "title":         title,
        "url":           url,
        "decision":      "YES" if is_rel else "NO",
        "reasoning":     reasoning,
        "raw_first_200": raw[:200],
    })

    return is_rel, reasoning, provider_name


def post_article(target_sub, entry, category, score, pos, neg, matched,
                 ai_used, ai_provider, ai_reasoning, paras, post_reason=""):
    flair_id   = get_flair_id(target_sub, category)
    ref        = generate_ref()
    safe_title = clean_text(entry.title)

    try:
        sub = target_sub.submit(title=safe_title, url=entry.link, flair_id=flair_id)

        lines = [f"**Source:** {entry.source}", ""]
        if paras:
            lines.extend([f"> {clean_text(p)}" for p in paras[:3]] + [""])
        lines.append("_Verified_")

        sub.reply('\n'.join(lines))
        add_to_dedup(entry)
        update_metrics(entry.source, category)

        log("POSTED", f"[{ref}] [{entry.source}] {safe_title[:55]}…", Col.GREEN)
        log("POSTED", f"  Score={score:+d}  Reason: {post_reason}", Col.GREEN)
        if ai_used and ai_reasoning:
            log("POSTED", f"  AI ({ai_provider}): {ai_reasoning}", Col.GREEN)

        append_encrypted_reasoning({
            "ts":           datetime.now(timezone.utc).isoformat(),
            "type":         "post",
            "ref":          ref,
            "subreddit":    target_sub.display_name,
            "source":       entry.source,
            "title":        safe_title,
            "url":          entry.link,
            "category":     category,
            "score":        score,
            "pos":          pos,
            "neg":          neg,
            "matched":      matched,
            "post_reason":  post_reason,
            "ai_used":      bool(ai_used),
            "ai_provider":  ai_provider,
            "ai_reasoning": ai_reasoning,
        })
        return True

    except Exception as e:
        log("ERROR", f"Post failed: {e}", Col.RED)
        return False


def handle_manual_story(url, title_override, subreddit_uk):
    log("MANUAL", f"Posting URL: {url}", Col.CYAN)
    paras = fetch_article_text(url)

    title = clean_text(title_override) if title_override else ""
    if not title:
        try:
            r    = requests.get(url, timeout=15, headers=REQUEST_HEADERS, allow_redirects=True)
            soup = BeautifulSoup(r.content, 'html.parser')
            og   = soup.find('meta', property='og:title')
            if og and og.get('content'):
                title = clean_text(og['content'])
            elif soup.title and soup.title.string:
                title = clean_text(soup.title.string)
        except Exception as e:
            log("MANUAL", f"Could not fetch page title: {e}", Col.YELLOW)
    if not title:
        title = clean_text(url.rstrip('/').split('/')[-1].replace('-', ' '))

    log("MANUAL", f"Title: {title[:70]}", Col.WHITE)

    summary   = " ".join(paras[:2]) if paras else ""
    entry     = NewsEntry("Manual", title, url, summary, datetime.now(timezone.utc))
    full_text = entry.title + " " + entry.summary + " " + " ".join(paras)

    score, pos, neg, matched = calculate_score(full_text)
    cat, _      = detect_category(full_text)
    post_reason = f"Manual submission (score {score:+d})"

    post_article(
        subreddit_uk, entry, cat, score, pos, neg, matched,
        ai_used=False, ai_provider="", ai_reasoning="",
        paras=paras, post_reason=post_reason
    )


def run_bot():
    global POSTED_URLS, POSTED_TITLES, POSTED_HASHES, _FERNET, AI_PROVIDERS
    global feedparser, requests, BeautifulSoup, dateparser

    try:
        import feedparser as _feedparser
        import requests as _requests
        from bs4 import BeautifulSoup as _BS4
        from dateutil import parser as _dateparser
    except ImportError as e:
        sys.exit(
            f"Missing dependency for bot mode: {e.name}.\n"
            f"Install: pip install feedparser requests beautifulsoup4 "
            f"praw python-dateutil google-genai cryptography"
        )
    feedparser    = _feedparser
    requests      = _requests
    BeautifulSoup = _BS4
    dateparser    = _dateparser

    reddit_required = [
        "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",  "REDDITPASSWORD",
    ]
    missing = [v for v in reddit_required if v not in os.environ]
    if missing:
        sys.exit(f"Missing env var(s): {', '.join(missing)}")

    _FERNET, enc_msg = _init_fernet_from_env()
    if _FERNET:
        log("REASONING", "Encrypted reasoning log ENABLED", Col.GREEN)
    else:
        log("REASONING", f"Encrypted reasoning log DISABLED ({enc_msg})", Col.DIM)

    groq_key   = os.environ.get("GROQ_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq   = GroqProvider(groq_key,     GROQ_MODEL,   GROQ_RPM)
    gemini = GeminiProvider(gemini_key, GEMINI_MODEL, GEMINI_RPM)
    AI_PROVIDERS = [groq, gemini]

    enabled_providers = [p.name for p in AI_PROVIDERS if p.enabled]
    if enabled_providers:
        log("AI", f"Provider chain: {' → '.join(enabled_providers)}", Col.GREEN)
    else:
        log("AI", "No AI providers configured — score-only decisions", Col.YELLOW)

    import praw
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDITPASSWORD"],
        user_agent="BreakingUKNewsBot/7.1"
    )
    try:
        log("SYSTEM", f"Logged in as: {reddit.user.me()}", Col.GREEN)
    except Exception as e:
        log("CRITICAL", f"Reddit login failed: {e}", Col.RED)
        sys.exit(1)

    subreddit_uk = reddit.subreddit("BreakingUKNews")

    POSTED_URLS, POSTED_TITLES, POSTED_HASHES = load_dedup()

    manual_url   = os.environ.get("MANUAL_STORY_URL",   "").strip()
    manual_title = os.environ.get("MANUAL_STORY_TITLE", "").strip()
    if manual_url:
        log("START", "Manual dispatch — single story post", Col.CYAN)
        handle_manual_story(manual_url, manual_title, subreddit_uk)
        return

    log("START", "=" * 60, Col.CYAN)
    log("START", "  Run starting", Col.CYAN)
    log("START", "=" * 60, Col.CYAN)

    feeds = [
        ("BBC-UK",        "https://feeds.bbci.co.uk/news/uk/rss.xml"),
        ("BBC-Politics",  "https://feeds.bbci.co.uk/news/politics/rss.xml"),
        ("BBC-Business",  "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("Sky-Home",      "https://feeds.skynews.com/feeds/rss/home.xml"),
        ("Sky-UK",        "https://feeds.skynews.com/feeds/rss/uk.xml"),
        ("Sky-Politics",  "https://feeds.skynews.com/feeds/rss/politics.xml"),
    ]
    cutoff      = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)
    raw_entries = []

    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
        except Exception as ex:
            log("FEED", f"{source}: fetch failed — {ex}", Col.RED)
            continue

        if getattr(feed, 'bozo', False) and not feed.entries:
            log("FEED", f"{source}: parse error — {getattr(feed, 'bozo_exception', '?')}", Col.RED)
            continue
        if not feed.entries:
            log("FEED", f"{source}: 0 entries", Col.YELLOW)
            continue

        kept, skipped_old, skipped_bad = 0, 0, 0
        for e in feed.entries:
            try:
                title = getattr(e, 'title', None)
                link  = getattr(e, 'link',  None)
                if not title or not link:
                    skipped_bad += 1
                    continue

                dt = None
                for k in ('published', 'updated'):
                    raw_date = getattr(e, k, None)
                    if raw_date:
                        try:
                            dt = dateparser.parse(raw_date)
                            break
                        except Exception:
                            continue

                if dt is not None:
                    if dt.tzinfo is None:
                        dt_utc = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt_utc = dt.astimezone(timezone.utc)
                    if dt_utc <= cutoff:
                        skipped_old += 1
                        continue
                else:
                    dt = datetime.now(timezone.utc)

                raw_entries.append(NewsEntry(
                    source, title, link, getattr(e, 'summary', ''), dt, e
                ))
                kept += 1
            except Exception as ex:
                skipped_bad += 1
                log("FEED", f"{source}: skipped bad entry — {ex}", Col.DIM)

        log("FEED",
            f"{source}: kept {kept}/{len(feed.entries)} "
            f"(too old: {skipped_old}, malformed: {skipped_bad})",
            Col.BLUE)

    raw_entries.sort(
        key=lambda x: x.published if x.published else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    log("INFO", f"Total articles to evaluate: {len(raw_entries)}", Col.WHITE)

    candidates, posted_titles_this_run = [], set()
    stats = {"duplicate": 0, "in_run_dup": 0, "rejected": 0,
             "accepted": 0, "ai_checked": 0, "ai_failed": 0}

    for entry in raw_entries:
        if len(candidates) >= INITIAL_ARTICLES:
            break

        norm_link  = normalize_url(entry.link)
        norm_title = normalize_title(entry.title)
        h          = content_hash(entry.title + entry.summary)

        if norm_link in POSTED_URLS or h in POSTED_HASHES:
            log("SKIP", f"[DUP]        {entry.title[:55]}…", Col.DIM)
            stats["duplicate"] += 1
            continue

        if any(difflib.SequenceMatcher(None, norm_title, t).ratio() > IN_RUN_FUZZY_THRESHOLD
               for t in posted_titles_this_run):
            log("SKIP", f"[IN-RUN-DUP] {entry.title[:55]}…", Col.DIM)
            stats["in_run_dup"] += 1
            continue

        log("PIPELINE", f"Evaluating: [{entry.source}] {entry.title[:60]}...", Col.CYAN)

        paras     = fetch_article_text(entry.link)
        full_text = entry.title + " " + entry.summary + " " + " ".join(paras)

        score, pos, neg, matched = calculate_score(full_text)
        cat, _         = detect_category(full_text)
        reject, reason = is_hard_reject(full_text, pos, neg)

        accept       = False
        ai_used      = False
        ai_provider  = ""
        ai_reasoning = ""
        post_reason  = ""

        if reject:
            post_reason = f"Hard reject: {reason}"
        else:
            has_uk_anchor  = any(g in full_text.lower()
                                 for g in ('uk', 'britain', 'london', 'england'))
            distinct_uk_kw = len([k for k in matched if not k.startswith("NEG:")])

            if score >= 15 and has_uk_anchor and distinct_uk_kw >= DISTINCT_UK_KW_REQUIRED:
                accept      = True
                post_reason = (
                    f"High UK score ({score:+d}) with UK anchor and "
                    f"{distinct_uk_kw} distinct UK keywords"
                )

            elif (score >= 15 and has_uk_anchor) or score >= 4:
                stats["ai_checked"] += 1
                is_rel, ai_reasoning, ai_provider = check_ai_relevance(
                    entry.title, entry.summary,
                    " ".join(full_text.split()[:200]), h,
                    source=entry.source, url=entry.link,
                )
                if is_rel is None:
                    stats["ai_failed"] += 1
                    post_reason = (
                        f"AI unavailable; score {score:+d} insufficient for blind accept"
                    )
                elif is_rel:
                    accept      = True
                    ai_used     = True
                    post_reason = (
                        f"Score {score:+d} (distinct UK kw={distinct_uk_kw}); "
                        f"AI [{ai_provider}] confirmed: {ai_reasoning}"
                    )
                else:
                    post_reason = (
                        f"Score {score:+d}; AI [{ai_provider}] rejected: {ai_reasoning}"
                    )
            else:
                post_reason = f"Score too low ({score:+d})"

        decision_label = "ACCEPT (UK)" if accept else "SKIP"
        log_score_detail(entry.title, score, pos, neg, matched, decision_label, post_reason)

        if accept:
            candidates.append({
                "entry":        entry,
                "score":        score,
                "pos":          pos,
                "neg":          neg,
                "cat":          cat,
                "matched":      matched,
                "ai_used":      ai_used,
                "ai_provider":  ai_provider,
                "ai_reasoning": ai_reasoning,
                "paras":        paras,
                "post_reason":  post_reason,
            })
            posted_titles_this_run.add(norm_title)
            stats["accepted"] += 1
        else:
            log("REJECTED", f"{entry.title[:55]}… — {post_reason}", Col.RED)
            stats["rejected"] += 1

    log("INFO", "=" * 60)
    log("INFO", f"Evaluation complete. Stats: {stats}")
    log("INFO", f"Attempting to post up to {TARGET_POSTS} articles…", Col.CYAN)

    posts_made    = 0
    source_counts = Counter()

    for c in candidates:
        if posts_made >= TARGET_POSTS:
            log("INFO", f"Reached target of {TARGET_POSTS} posts.", Col.GREEN)
            break

        src = c["entry"].source
        if source_counts[src] >= MAX_PER_SOURCE:
            log("SKIP", f"Max ({MAX_PER_SOURCE}) reached for source: {src}", Col.DIM)
            continue

        if post_article(
            target_sub=subreddit_uk,
            entry=c["entry"],
            category=c["cat"],
            score=c["score"],
            pos=c["pos"],
            neg=c["neg"],
            matched=c["matched"],
            ai_used=c["ai_used"],
            ai_provider=c["ai_provider"],
            ai_reasoning=c["ai_reasoning"],
            paras=c["paras"],
            post_reason=c["post_reason"],
        ):
            posts_made += 1
            source_counts[src] += 1
            time.sleep(2)

    log("INFO", f"Run finished. Posts made: {posts_made}", Col.GREEN)


def run_decrypt(argv):
    path = argv[0] if argv else REASONING_LOG_FILE
    if not os.path.exists(path):
        sys.exit(f"No log file: {path}")
    passcode = os.environ.get("REASONING_PASSCODE") or getpass.getpass("Passcode: ")
    if not passcode:
        sys.exit("No passcode")
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        sys.exit("Run: pip install cryptography")
    fernet  = Fernet(_derive_fernet_key(passcode))
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                pt = fernet.decrypt(line.encode("utf-8"))
            except InvalidToken:
                sys.exit(f"Decryption failed at line {i}")
            try:
                records.append(json.loads(pt))
            except json.JSONDecodeError:
                pass
    json.dump(records, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("decrypt", "decrypt-log", "--decrypt", "--decrypt-log"):
        run_decrypt(argv[1:])
        return
    run_bot()


if __name__ == "__main__":
    main()
