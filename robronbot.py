import os
import re
import sys
import html
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import praw


def _env(key, default):
    val = os.environ.get(key)
    return val if val not in (None, "") else default


TVMAZE_SHOW_ID = int(_env("TVMAZE_SHOW_ID", "2548"))
TVMAZE_BASE = "https://api.tvmaze.com"
LOCAL_TZ = _env("LOCAL_TZ", "Europe/London")
SUBREDDIT = _env("SUBREDDIT", "robronaddicts")

SITE_BASE = _env("SITE_BASE", "https://www.tvguide.co.uk")
SITE_SECTION_HINT = _env("SITE_SECTION_HINT", "/emmerdale-insider/")
SPOILER_INDEX_URL = _env("SPOILER_INDEX_URL", f"{SITE_BASE}/emmerdale-insider/category/spoilers/")
# Section index pages we sweep for new Robron mentions.
SITE_SECTION_URLS = [u.strip() for u in _env(
    "SITE_SECTION_URLS",
    f"{SITE_BASE}/emmerdale-insider/category/spoilers/,"
    f"{SITE_BASE}/emmerdale-insider/category/news/,"
    f"{SITE_BASE}/emmerdale-insider/category/episode-recaps/",
).split(",") if u.strip()]
# Don't surface an article older than this many days.
ROBRON_MENTION_MAX_AGE_DAYS = int(_env("ROBRON_MENTION_MAX_AGE_DAYS", "3"))
# How many candidate articles to fetch per sweep at most (politeness + speed).
ROBRON_MAX_ARTICLES_PER_SWEEP = int(_env("ROBRON_MAX_ARTICLES_PER_SWEEP", "12"))

ROBRON_TERMS = [t.strip().lower() for t in _env("ROBRON_TERMS", "robron,aaron dingle,robert sugden,aaron,robert").split(",") if t.strip()]

POST_EP_HOUR = int(_env("POST_EP_HOUR", "7"))
POST_EP_MINUTE = int(_env("POST_EP_MINUTE", "0"))
POST_EP_WINDOW_MIN = int(_env("POST_EP_WINDOW_MIN", "90"))
POST_EP_WEEKDAYS = set(int(d) for d in _env("POST_EP_WEEKDAYS", "0,1,2,3,4").split(",") if d.strip())

# Hours (UK local) at which to sweep for new forward-spoiler articles and new Robron mentions.
SWEEP_HOURS = [int(h) for h in _env("SWEEP_HOURS", "7,18").split(",") if h.strip()]
SWEEP_WINDOW_MIN = int(_env("SWEEP_WINDOW_MIN", "90"))
SPOILER_HEADLINE_MAX_WORDS = int(_env("SPOILER_HEADLINE_MAX_WORDS", "14"))

JOB = _env("JOB", "auto")
FORCE_WINDOW = _env("FORCE_WINDOW", "false").lower() == "true"

POST_FLAIR_POST_EP = _env("POST_FLAIR_POST_EP", "Post-Episode Discussion ")
POST_FLAIR_SPOILER = _env("POST_FLAIR_SPOILER", "Spoilers")
STICKY = _env("STICKY", "true").lower() == "true"
STICKY_SLOT = int(_env("STICKY_SLOT", "2"))
SUGGESTED_SORT = _env("SUGGESTED_SORT", "new")

DEDUPE_SCAN_LIMIT = int(_env("DEDUPE_SCAN_LIMIT", "80"))
ROBRON_COMMENT_SCAN_LIMIT = int(_env("ROBRON_COMMENT_SCAN_LIMIT", "100"))

USE_GEMINI_INTRO = _env("USE_GEMINI_INTRO", "false").lower() == "true"
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-1.5-flash")

DRY_RUN = _env("DRY_RUN", "false").lower() == "true"
USER_AGENT = _env("USER_AGENT", "python:robronaddicts-episode-bot:v5.0 (by /u/robronaddicts mods)")

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.google.com/",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], start=1)}


def strip_html(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def now_local():
    return datetime.now(ZoneInfo(LOCAL_TZ))


def _in_window(when, hour, minute, window_min):
    minutes_now = when.hour * 60 + when.minute
    start = hour * 60 + minute
    return start <= minutes_now <= start + window_min


def _http_get(url, timeout=20):
    return requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)


def parse_label_date(label, ref):
    if not label:
        return None
    low = label.lower()
    if "-" in low or "\u2013" in low:
        return None
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})\b", low)
    if not m:
        return None
    month, day = MONTHS[m.group(1)], int(m.group(2))
    best = None
    for yr in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = date(yr, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - ref).days) < abs((best - ref).days):
            best = cand
    return best


def has_robron(text):
    if not text:
        return False
    t = text.lower()
    return any(re.search(r"\b" + re.escape(term) + r"\b", t) for term in ROBRON_TERMS)


def slug_from_url(url):
    return url.rstrip("/").split("/")[-1]


def _index_records(html_text, ref):
    soup = BeautifulSoup(html_text, "html.parser")
    order, seen = [], {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/emmerdale-insider/spoilers/" not in href or href.rstrip("/").endswith("category/spoilers"):
            continue
        rec = seen.get(href)
        if rec is None:
            rec = {"url": href, "date": None, "headline": "", "is_range": False}
            seen[href] = rec
            order.append(rec)
        txt = a.get_text(" ", strip=True)
        d = parse_label_date(txt, ref)
        if d:
            rec["date"] = d
        else:
            low = txt.lower()
            if "-" in low or "\u2013" in low or "upcoming" in low:
                rec["is_range"] = True
            if len(txt) > len(rec["headline"]):
                rec["headline"] = txt
    return order


def get_forward_spoiler(html_text, today):
    for rec in _index_records(html_text, today):
        hl = rec["headline"].lower()
        forward = (rec["is_range"] or "next week" in hl or "upcoming" in hl
                   or (rec["date"] and rec["date"] > today))
        if forward:
            return rec["url"], rec["headline"]
    return None, None


# --- whole-site Robron sweep ----------------------------------------------

def _collect_article_links(html_text, base_url):
    """Return [(absolute_url, link_text)] from any anchor pointing into the Emmerdale Insider section."""
    soup = BeautifulSoup(html_text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if SITE_SECTION_HINT not in href:
            continue
        absu = urljoin(base_url, href).split("#", 1)[0]
        if absu.rstrip("/").endswith("/category") or "/category/" in absu:
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append((absu, a.get_text(" ", strip=True)))
    return out


def _extract_pubdate(soup):
    for sel in ("meta[property='article:published_time']",
                "meta[name='article:published_time']",
                "meta[property='og:updated_time']",
                "time[datetime]"):
        el = soup.select_one(sel)
        if not el:
            continue
        val = el.get("content") or el.get("datetime")
        if not val:
            continue
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
        except Exception:
            pass
    return None


def _article_snippet(soup, max_chars=240):
    md = soup.select_one("meta[name='description']") or soup.select_one("meta[property='og:description']")
    if md and md.get("content"):
        s = strip_html(md["content"])
        if s:
            return s[:max_chars]
    p = soup.select_one("article p, main p, p")
    return strip_html(p.get_text(" ", strip=True)) [:max_chars] if p else ""


@dataclass
class RobronMention:
    url: str
    title: str
    snippet: str
    pubdate: "date | None"


def sweep_robron_mentions(today, http_get=None, max_articles=None):
    """Crawl section index pages, return [RobronMention] for recent articles naming Robron."""
    http = http_get or _http_get
    max_articles = max_articles or ROBRON_MAX_ARTICLES_PER_SWEEP
    candidates, candidate_urls = [], set()

    for section in SITE_SECTION_URLS:
        try:
            resp = http(section)
            if hasattr(resp, "status_code") and resp.status_code != 200:
                print(f"[sweep] {section} HTTP {resp.status_code}")
                continue
            text = resp.text if hasattr(resp, "text") else resp
        except Exception as e:
            print(f"[sweep] {section} failed: {e}")
            continue
        for url, link_text in _collect_article_links(text, section):
            if url in candidate_urls:
                continue
            candidate_urls.add(url)
            candidates.append((url, link_text))
            # Headline-stage filter to limit how many we fetch.
            if has_robron(link_text):
                candidates[-1] = (url, link_text)

    # Prefer those with Robron in the link text -- cheaper signal.
    candidates.sort(key=lambda x: 0 if has_robron(x[1]) else 1)
    candidates = candidates[:max_articles]

    mentions = []
    for url, link_text in candidates:
        try:
            resp = http(url)
            if hasattr(resp, "status_code") and resp.status_code != 200:
                continue
            body = resp.text if hasattr(resp, "text") else resp
        except Exception:
            continue
        soup = BeautifulSoup(body, "html.parser")
        title = (soup.title.get_text(strip=True) if soup.title else "") or link_text
        text = soup.get_text(" ", strip=True)
        if not has_robron(text):
            continue
        pubdate = _extract_pubdate(soup)
        if pubdate and (today - pubdate).days > ROBRON_MENTION_MAX_AGE_DAYS:
            continue
        snippet = _article_snippet(soup)
        mentions.append(RobronMention(url=url, title=title, snippet=snippet, pubdate=pubdate))

    return mentions


# --- TVMaze / dataclass (kept for any future re-use) -----------------------

@dataclass
class Episode:
    season: int
    number: int
    airdate: str
    name: str
    summary_text: str

    @property
    def code(self):
        return f"S{self.season}E{self.number}"


def fetch_episodes(session=None, retries=4):
    sess = session or requests.Session()
    url = f"{TVMAZE_BASE}/shows/{TVMAZE_SHOW_ID}/episodes"
    last_err = None
    for attempt in range(retries):
        try:
            resp = sess.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=20)
            if resp.status_code == 429:
                time.sleep(2 ** attempt); continue
            resp.raise_for_status()
            return [
                Episode(int(e.get("season") or 0), int(e.get("number") or 0),
                        e.get("airdate") or "", e.get("name") or "",
                        strip_html(e.get("summary")))
                for e in resp.json()
            ]
        except Exception as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"TVMaze fetch failed: {last_err}")


# --- scheduling ------------------------------------------------------------

def decide_jobs(when=None):
    when = when or now_local()
    jobs = []
    if (when.weekday() in POST_EP_WEEKDAYS
            and _in_window(when, POST_EP_HOUR, POST_EP_MINUTE, POST_EP_WINDOW_MIN)):
        jobs.append("post_episode")
    for h in SWEEP_HOURS:
        if _in_window(when, h, 0, SWEEP_WINDOW_MIN):
            jobs.append("sweep")
            break
    return jobs


# --- post bodies -----------------------------------------------------------

def _spoiler(text):
    safe = text.replace("!<", "! <").replace(">!", "> !")
    return f">!{safe}!<"


SPOILER_RULE = (
    "**Please keep spoilers out of the title and use spoiler tags** "
    "`>!like this!<` **in comments.**"
)


def build_post_episode(date_obj):
    weekday = date_obj.strftime("%A")
    date_s = date_obj.strftime("%-d %B %Y")
    title = f"Post-Episode Discussion for {weekday} {date_s}"
    body = (
        "I have spoilered this for anyone wanting to talk about the early "
        "release on YouTube.\n\n"
        + SPOILER_RULE
    )
    return title, body


def build_spoilers(headline, source_url, date_obj):
    date_s = date_obj.strftime("%-d %B %Y")
    title = f"Spoilers & Rumours \u2014 Upcoming Emmerdale ({date_s})"
    if headline and 0 < len(headline.split()) <= SPOILER_HEADLINE_MAX_WORDS:
        covered = _spoiler(f"\u201c{headline}\u201d \u2014 via Emmerdale Insider")
    else:
        covered = _spoiler("New forward spoilers have been published \u2014 see the source link below.")
    lines = [
        "New spoilers for upcoming Emmerdale episodes have been published. Robron "
        "don't always feature \u2014 drop anything in that does, plus any rumours, here.",
        "",
        "Preview (spoiler-tagged): " + covered,
        "",
    ]
    if source_url:
        lines.append(f"[Full spoilers at the source]({source_url}) (external; spoilers).")
        lines.append("")
    lines += [SPOILER_RULE, "", "Please credit/link the original source where you can."]
    return title, "\n".join(lines).strip()


def build_mentions_comment(mentions):
    lines = [
        "**Robron in today's Emmerdale Insider coverage** \u2014 fresh articles "
        "mentioning Robert / Aaron / Robron. Spoiler-tagged previews; full pieces "
        "at the source links (external; spoilers).",
        "",
    ]
    for m in mentions:
        date_tag = f" ({m.pubdate.strftime('%-d %b')})" if m.pubdate else ""
        clean_title = re.sub(r"\s+\|\s+TV Guide.*$", "", m.title or "").strip()
        lines.append(f"* [{clean_title or m.url}]({m.url}){date_tag}")
        if m.snippet:
            lines.append(f"  * Preview: {_spoiler(m.snippet)}")
    return "\n".join(lines)


# --- reddit ----------------------------------------------------------------

def make_reddit():
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDITPASSWORD"],
        user_agent=USER_AGENT,
    )
    reddit.validate_on_submit = True
    return reddit


def _recent_bot_posts(reddit):
    out = []
    for submission in reddit.user.me().submissions.new(limit=DEDUPE_SCAN_LIMIT):
        if submission.subreddit.display_name.lower() == SUBREDDIT.lower():
            out.append(submission)
    return out


def post_episode_title_prefix(date_obj):
    return f"Post-Episode Discussion for {date_obj.strftime('%A')} {date_obj.strftime('%-d %B %Y')}"


def post_episode_already_posted(reddit, title_prefix):
    return any((s.title or "").startswith(title_prefix) for s in _recent_bot_posts(reddit))


def find_today_post_episode(reddit, date_obj):
    prefix = post_episode_title_prefix(date_obj)
    for s in _recent_bot_posts(reddit):
        if (s.title or "").startswith(prefix):
            return s
    return None


def spoiler_already_posted(reddit, source_url):
    return any(source_url and source_url in (s.selftext or "") for s in _recent_bot_posts(reddit))


def urls_already_commented(submission):
    seen = set()
    try:
        submission.comments.replace_more(limit=0)
        for c in submission.comments.list()[:ROBRON_COMMENT_SCAN_LIMIT]:
            body = getattr(c, "body", "") or ""
            for u in re.findall(r"https?://[^\s)\]]+", body):
                seen.add(u.rstrip("/"))
    except Exception as e:
        print(f"[warn] could not scan comments: {e}")
    return seen


def _apply_flair(submission, flair_text):
    if not flair_text:
        return
    for template in submission.subreddit.flair.link_templates:
        if template.get("text", "").strip().lower() == flair_text.strip().lower():
            submission.flair.select(template["id"])
            return
    submission.mod.flair(text=flair_text)


JOB_FLAIRS = {"post_episode": "POST_FLAIR_POST_EP", "spoilers": "POST_FLAIR_SPOILER"}


def submit_thread(reddit, title, body, job, spoiler=False):
    sub = reddit.subreddit(SUBREDDIT)
    submission = sub.submit(title=title, selftext=body, send_replies=False)
    if spoiler:
        try:
            submission.mod.spoiler()
        except Exception as e:
            print(f"[warn] could not mark spoiler: {e}")
    try:
        submission.mod.suggested_sort(SUGGESTED_SORT)
    except Exception as e:
        print(f"[warn] suggested sort: {e}")
    flair = globals().get(JOB_FLAIRS.get(job, ""), "")
    try:
        _apply_flair(submission, flair)
    except Exception as e:
        print(f"[warn] flair '{flair}': {e}")
    if STICKY:
        try:
            submission.mod.sticky(state=True, bottom=(STICKY_SLOT == 2))
        except Exception as e:
            print(f"[warn] sticky: {e}")
    return submission


def resolve_jobs(now):
    if JOB in ("post_episode", "sweep"):
        if FORCE_WINDOW or JOB in decide_jobs(now):
            return [JOB]
        print(f"[skip] forced job '{JOB}' is outside its window (set FORCE_WINDOW=true).")
        return []
    if JOB == "auto":
        return decide_jobs(now)
    print(f"[error] invalid JOB={JOB!r}")
    return []


# --- run --------------------------------------------------------------------

def run_post_episode(reddit, now):
    date_str = now.strftime("%Y-%m-%d")
    title, body = build_post_episode(now)
    prefix = post_episode_title_prefix(now)
    if DRY_RUN:
        print("=== DRY RUN [post_episode] ===\nTITLE:", title, "\n---- BODY ----\n" + body)
        return reddit
    if reddit is None:
        reddit = make_reddit()
    if post_episode_already_posted(reddit, prefix):
        print(f"[skip] already posted {date_str}/post_episode.")
        return reddit
    sub = submit_thread(reddit, title, body, "post_episode", spoiler=True)
    print(f"[ok] posted [post_episode]: https://redd.it/{sub.id}")
    return reddit


def run_sweep(reddit, now):
    # Forward-spoiler thread first.
    try:
        resp = _http_get(SPOILER_INDEX_URL)
        if resp.status_code == 200:
            url, headline = get_forward_spoiler(resp.text, now.date())
            if url:
                slug = slug_from_url(url)
                print(f"[detect] forward spoilers found: {slug}")
                stitle, sbody = build_spoilers(headline, url, now)
                if DRY_RUN:
                    print("=== DRY RUN [spoilers] ===\nTITLE:", stitle, "\n---- BODY ----\n" + sbody)
                else:
                    if reddit is None:
                        reddit = make_reddit()
                    if not spoiler_already_posted(reddit, url):
                        sub = submit_thread(reddit, stitle, sbody, "spoilers")
                        print(f"[ok] posted [spoilers]: https://redd.it/{sub.id}")
                    else:
                        print(f"[skip] already posted spoilers for {slug}.")
            else:
                print("[detect] no new forward-spoiler article.")
        else:
            print(f"[warn] spoiler index HTTP {resp.status_code}")
    except Exception as e:
        print(f"[warn] spoiler sweep failed: {e}")

    # Robron mentions comment.
    try:
        mentions = sweep_robron_mentions(now.date())
    except Exception as e:
        print(f"[warn] robron sweep failed: {e}")
        mentions = []
    print(f"[detect] Robron mentions found: {len(mentions)}")
    if not mentions:
        return reddit

    if DRY_RUN:
        print("=== DRY RUN [robron mentions] ===")
        for m in mentions:
            print(f"  - {m.title} :: {m.url}")
        print("\n---- COMMENT BODY ----\n" + build_mentions_comment(mentions))
        return reddit

    if reddit is None:
        reddit = make_reddit()
    target = find_today_post_episode(reddit, now)
    if target is None:
        print("[skip] no post-episode thread today to attach mentions to.")
        return reddit
    already = urls_already_commented(target)
    fresh = [m for m in mentions if m.url.rstrip("/") not in already]
    if not fresh:
        print("[skip] all Robron mentions already commented on today's thread.")
        return reddit
    body = build_mentions_comment(fresh)
    try:
        c = target.reply(body)
        print(f"[ok] commented {len(fresh)} mention(s) on https://redd.it/{target.id} :: {getattr(c,'id','?')}")
    except Exception as e:
        print(f"[error] reply failed: {e}")
    return reddit


def main():
    now = now_local()
    jobs = resolve_jobs(now)
    if not jobs:
        print(f"[skip] {now:%a %H:%M %Z} \u2014 nothing scheduled.")
        return 0
    print(f"[info] UK {now:%a %Y-%m-%d %H:%M} \u2014 jobs: {', '.join(jobs)}")
    reddit = None
    for job in jobs:
        if job == "post_episode":
            reddit = run_post_episode(reddit, now)
        elif job == "sweep":
            reddit = run_sweep(reddit, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
