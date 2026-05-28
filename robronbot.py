import os
import re
import sys
import html
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import praw


def _env(key, default):
    val = os.environ.get(key)
    return val if val not in (None, "") else default


TVMAZE_SHOW_ID = int(_env("TVMAZE_SHOW_ID", "2548"))
TVMAZE_BASE = "https://api.tvmaze.com"
LOCAL_TZ = _env("LOCAL_TZ", "Europe/London")
SUBREDDIT = _env("SUBREDDIT", "robronaddicts")

PHASES = {
    "early": {
        "label": "Early Access (ITVX)",
        "target_hour": int(_env("EARLY_HOUR", "7")),
        "target_minute": int(_env("EARLY_MINUTE", "0")),
        "window_minutes": int(_env("EARLY_WINDOW_MIN", "90")),
        "when_line": "Episodes drop on **ITVX** in the morning ahead of the TV broadcast.",
    },
    "broadcast": {
        "label": "On Air (ITV1)",
        "target_hour": int(_env("BROADCAST_HOUR", "20")),
        "target_minute": int(_env("BROADCAST_MINUTE", "0")),
        "window_minutes": int(_env("BROADCAST_WINDOW_MIN", "90")),
        "when_line": "Airing now on **ITV1** (8pm).",
    },
}

PHASE = _env("PHASE", "auto")
FORCE_PHASE = _env("FORCE_PHASE", "false").lower() == "true"

TITLE_TEMPLATE = _env(
    "TITLE_TEMPLATE",
    "Emmerdale Discussion \u2014 {weekday} {date} \u2014 S{season}E{ep_in_season} [{phase_label}]",
)
TITLE_TEMPLATE_MULTI = _env(
    "TITLE_TEMPLATE_MULTI",
    "Emmerdale Discussion \u2014 {weekday} {date} \u2014 {ep_list} [{phase_label}]",
)

POST_FLAIR = _env("POST_FLAIR", "Episode Discussion")
STICKY = _env("STICKY", "true").lower() == "true"
STICKY_SLOT = int(_env("STICKY_SLOT", "2"))
SUGGESTED_SORT = _env("SUGGESTED_SORT", "new")

MARKER_PREFIX = _env("MARKER_PREFIX", "emmerbot")
DEDUPE_SCAN_LIMIT = int(_env("DEDUPE_SCAN_LIMIT", "50"))

USE_GEMINI_INTRO = _env("USE_GEMINI_INTRO", "false").lower() == "true"
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-1.5-flash")

DRY_RUN = _env("DRY_RUN", "false").lower() == "true"
USER_AGENT = _env("USER_AGENT", "python:robronaddicts-episode-bot:v1.0 (by /u/robronaddicts mods)")


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


def strip_html(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def now_local():
    return datetime.now(ZoneInfo(LOCAL_TZ))


def active_phase(when=None):
    when = when or now_local()
    minutes_now = when.hour * 60 + when.minute
    for name, p in PHASES.items():
        start = p["target_hour"] * 60 + p["target_minute"]
        if start <= minutes_now <= start + p["window_minutes"]:
            return name
    return None


def fetch_episodes(session=None, retries=4):
    sess = session or requests.Session()
    url = f"{TVMAZE_BASE}/shows/{TVMAZE_SHOW_ID}/episodes"
    last_err = None
    for attempt in range(retries):
        try:
            resp = sess.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=20,
            )
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return [
                Episode(
                    season=int(e.get("season") or 0),
                    number=int(e.get("number") or 0),
                    airdate=e.get("airdate") or "",
                    name=e.get("name") or "",
                    summary_text=strip_html(e.get("summary")),
                )
                for e in resp.json()
            ]
        except Exception as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch TVMaze episodes after {retries} tries: {last_err}")


def episodes_for_date(episodes, date_str):
    found = [e for e in episodes if e.airdate == date_str]
    return sorted(found, key=lambda e: (e.season, e.number))


def build_title(eps, phase, date_obj):
    phase_label = PHASES[phase]["label"]
    weekday = date_obj.strftime("%A")
    date = date_obj.strftime("%-d %B %Y")
    first = eps[0]
    if len(eps) == 1:
        return TITLE_TEMPLATE.format(
            weekday=weekday, date=date, season=first.season,
            ep_in_season=first.number, phase_label=phase_label, ep_list=first.code,
        )
    ep_list = " & ".join(e.code for e in eps)
    return TITLE_TEMPLATE_MULTI.format(
        weekday=weekday, date=date, season=first.season,
        ep_in_season=first.number, phase_label=phase_label, ep_list=ep_list,
    )


def _spoiler(text):
    safe = text.replace("!<", "! <").replace(">!", "> !")
    return f">!{safe}!<"


def build_body(eps, phase, date_obj):
    phase_label = PHASES[phase]["label"]
    when_line = PHASES[phase].get("when_line", "")
    lines = [
        f"Discussion thread for tonight's Emmerdale \u2014 **{phase_label}**.",
        "",
        when_line,
        "",
        "Talk about everything Robron (and the wider episode) here. "
        "**Please keep spoilers for *future* episodes out of the title and use "
        "spoiler tags** `>!like this!<` in comments.",
        "",
        "---",
        "",
    ]
    for e in eps:
        lines.append(f"**{e.code}** \u2014 {e.name}".rstrip(" \u2014"))
        lines.append("")
        if e.summary_text:
            lines.append("Synopsis (spoiler-tagged): " + _spoiler(e.summary_text))
        else:
            lines.append("_No synopsis available yet._")
        lines.append("")

    if USE_GEMINI_INTRO:
        intro = _gemini_intro()
        if intro:
            lines = [intro, ""] + lines
    return "\n".join(lines).strip()


def _gemini_intro():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    try:
        prompt = (
            "Write ONE short, warm, spoiler-free sentence opening a fan discussion "
            "thread for tonight's episode of the soap Emmerdale, focused on the couple "
            "'Robron' (Robert Sugden and Aaron Dingle). Do NOT invent any plot details, "
            "names, or events. No emojis. Under 25 words."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={key}"
        )
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[warn] Gemini intro failed: {e}")
        return ""


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


def marker(date_str, phase):
    return f"{MARKER_PREFIX}:{date_str}:{phase}"


def already_posted(reddit, date_str, phase):
    needle = marker(date_str, phase)
    for submission in reddit.user.me().submissions.new(limit=DEDUPE_SCAN_LIMIT):
        if submission.subreddit.display_name.lower() != SUBREDDIT.lower():
            continue
        if needle in (submission.selftext or ""):
            return True
    return False


def _apply_flair(submission, flair_text):
    for template in submission.subreddit.flair.link_templates:
        if template.get("text", "").strip().lower() == flair_text.strip().lower():
            submission.flair.select(template["id"])
            return
    submission.mod.flair(text=flair_text)


def submit_thread(reddit, title, body, date_str, phase):
    sub = reddit.subreddit(SUBREDDIT)
    full_body = f"{body}\n\n&#32;\n\n^({marker(date_str, phase)})"
    submission = sub.submit(title=title, selftext=full_body, send_replies=False)

    try:
        submission.mod.suggested_sort(SUGGESTED_SORT)
    except Exception as e:
        print(f"[warn] suggested sort: {e}")

    if POST_FLAIR:
        try:
            _apply_flair(submission, POST_FLAIR)
        except Exception as e:
            print(f"[warn] flair '{POST_FLAIR}': {e}")

    if STICKY:
        try:
            submission.mod.sticky(state=True, bottom=(STICKY_SLOT == 2))
        except Exception as e:
            print(f"[warn] sticky: {e}")

    return submission


def resolve_phase():
    if PHASE in ("early", "broadcast"):
        return PHASE
    if PHASE == "auto":
        return active_phase()
    print(f"[error] invalid PHASE={PHASE!r}")
    return None


def main():
    now = now_local()
    date_str = now.strftime("%Y-%m-%d")

    phase = resolve_phase()
    if phase is None:
        print(f"[skip] {now:%H:%M %Z} outside any posting window.")
        return 0
    print(f"[info] UK date {date_str}, phase '{phase}'.")

    if PHASE in ("early", "broadcast") and active_phase() != phase and not FORCE_PHASE:
        print(f"[skip] forced phase '{phase}' outside its window (set FORCE_PHASE=true to override).")
        return 0

    try:
        episodes = fetch_episodes()
    except Exception as e:
        print(f"[error] schedule fetch failed: {e}")
        return 1

    todays = episodes_for_date(episodes, date_str)
    if not todays:
        print(f"[skip] no Emmerdale episode listed for {date_str}.")
        return 0
    print(f"[info] episode(s) today: {', '.join(e.code for e in todays)}")

    title = build_title(todays, phase, now)
    body = build_body(todays, phase, now)

    if DRY_RUN:
        print("=== DRY RUN ===")
        print("TITLE:", title)
        print("---- BODY ----")
        print(body)
        return 0

    reddit = make_reddit()
    if already_posted(reddit, date_str, phase):
        print(f"[skip] already posted {date_str}/{phase}.")
        return 0

    submission = submit_thread(reddit, title, body, date_str, phase)
    print(f"[ok] posted: https://redd.it/{submission.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
