"""
Built-in test suite for robronbot.

Offline tests run with no network and no secrets. Live tests (LIVE_TESTS=1)
fetch the spoiler site and TVMaze to confirm the real integrations still parse.

Run:
    python -m unittest test_robronbot -v
    LIVE_TESTS=1 python -m unittest test_robronbot -v
"""

import os
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import robronbot as rb

UK = ZoneInfo("Europe/London")

INDEX = """
<html><body>
<a href="https://x/emmerdale-insider/spoilers/gabby-ross-jai/"><img/> Fri, May 29</a>
<h3><a href="https://x/emmerdale-insider/spoilers/gabby-ross-jai/">2 spoilers for tonight: Gabby puts it all out there for Ross and Jai grovels</a></h3>

<a href="https://x/emmerdale-insider/spoilers/robron-find-out/"><img/> Thu, May 28</a>
<h3><a href="https://x/emmerdale-insider/spoilers/robron-find-out/">3 spoilers: Robert and Aaron are determined to find out the truth</a></h3>

<a href="https://x/emmerdale-insider/spoilers/kammy-belle/"><img/> Wed, May 27</a>
<h3><a href="https://x/emmerdale-insider/spoilers/kammy-belle/">3 spoilers for Wed, May 27: Kammy caught out and Ross wants Laurel</a></h3>

<a href="https://x/emmerdale-insider/spoilers/next-week-roundup/"><img/> Jun 1 - 5</a>
<h3><a href="https://x/emmerdale-insider/spoilers/next-week-roundup/">11 Emmerdale spoilers for next week: Todd exposes a secret</a></h3>

<a href="https://x/emmerdale-insider/category/spoilers/">Spoilers</a>
</body></html>
"""

ARTICLES = {
    "https://x/emmerdale-insider/spoilers/gabby-ross-jai/":
        "<p>Gabby confronts Ross at the cafe. Jai apologises to Laurel.</p>",
    "https://x/emmerdale-insider/spoilers/kammy-belle/":
        "<p>Kammy snaps at Belle. Robert overhears and tells Aaron that evening.</p>",
}


def fake_fetch(url):
    return ARTICLES.get(url, "<p>nothing here</p>")


class TestDateParsing(unittest.TestCase):
    def test_simple(self):
        ref = date(2026, 5, 29)
        self.assertEqual(rb.parse_label_date("Fri, May 29", ref), date(2026, 5, 29))

    def test_ranges_and_junk_rejected(self):
        ref = date(2026, 5, 29)
        self.assertIsNone(rb.parse_label_date("Jun 1 - 5", ref))
        self.assertIsNone(rb.parse_label_date("Upcoming", ref))

    def test_year_rollover(self):
        self.assertEqual(rb.parse_label_date("Sat, Jan 2", date(2026, 12, 31)), date(2027, 1, 2))


class TestNameDetection(unittest.TestCase):
    def test_positive(self):
        self.assertTrue(rb.has_robron("Robert and Aaron catch Kammy"))
        self.assertTrue(rb.has_robron("Robron reunite"))

    def test_negative(self):
        self.assertFalse(rb.has_robron("Kammy arrested and Laurel can't stay away from Ross"))
        self.assertFalse(rb.has_robron("Cain Dingle leaves Victoria devastated"))


class TestDetectRobron(unittest.TestCase):
    def test_named_in_headline(self):
        state, _, reason = rb.detect_robron(date(2026, 5, 28), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIs(state, True)
        self.assertIn("headline", reason)

    def test_named_in_body(self):
        state, _, reason = rb.detect_robron(date(2026, 5, 27), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIs(state, True)
        self.assertIn("body", reason)

    def test_found_but_not_named(self):
        state, _, _ = rb.detect_robron(date(2026, 5, 29), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIs(state, False)

    def test_no_entry(self):
        state, _, _ = rb.detect_robron(date(2026, 6, 30), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIsNone(state)


class TestForwardSpoilerDetection(unittest.TestCase):
    def test_finds_next_week_roundup(self):
        url, headline = rb.get_forward_spoiler(INDEX, date(2026, 5, 29))
        self.assertIsNotNone(url)
        self.assertIn("next-week-roundup", url)
        self.assertEqual(rb.slug_from_url(url), "next-week-roundup")

    def test_none_when_only_past_dates(self):
        past_only = """
        <a href="https://x/emmerdale-insider/spoilers/old/"><img/> Mon, May 25</a>
        <h3><a href="https://x/emmerdale-insider/spoilers/old/">Cain arrested</a></h3>
        """
        url, _ = rb.get_forward_spoiler(past_only, date(2026, 5, 29))
        self.assertIsNone(url)


class TestOverride(unittest.TestCase):
    def setUp(self):
        self._orig = rb.ROBRON_OVERRIDE

    def tearDown(self):
        rb.ROBRON_OVERRIDE = self._orig

    def test_on(self):
        rb.ROBRON_OVERRIDE = "on"
        self.assertTrue(rb.is_robron_day(date(2030, 1, 1))[0])

    def test_off(self):
        rb.ROBRON_OVERRIDE = "off"
        self.assertFalse(rb.is_robron_day(date(2030, 1, 1))[0])


class TestScheduling(unittest.TestCase):
    def test_episode_and_spoilers_in_morning(self):
        jobs = rb.decide_jobs(datetime(2026, 5, 29, 7, 10, tzinfo=UK))
        self.assertIn("episode", jobs)
        self.assertIn("spoilers", jobs)

    def test_evening_spoilers_only(self):
        jobs = rb.decide_jobs(datetime(2026, 5, 29, 18, 20, tzinfo=UK))
        self.assertEqual(jobs, ["spoilers"])

    def test_midday_nothing(self):
        self.assertEqual(rb.decide_jobs(datetime(2026, 5, 29, 12, 0, tzinfo=UK)), [])


class TestBuilds(unittest.TestCase):
    def test_episode_with_enrichment(self):
        eps = [rb.Episode(55, 108, "2026-05-29", "Friday", "Robert makes a decision that stuns Aaron.")]
        title, body = rb.build_episode(eps, datetime(2026, 5, 29, 7, 0, tzinfo=UK), source_url="https://x/s/")
        self.assertIn("S55E108", title)
        self.assertIn(">!", body)
        self.assertIn("Robert makes a decision that stuns Aaron.", body)
        self.assertIn("https://x/s/", body)

    def test_episode_double_bill(self):
        eps = [rb.Episode(55, 108, "2026-05-29", "A", ""), rb.Episode(55, 109, "2026-05-29", "B", "")]
        title, _ = rb.build_episode(eps, datetime(2026, 5, 29, 7, 0, tzinfo=UK))
        self.assertIn("S55E108 & S55E109", title)

    def test_spoilers_has_working_cover_and_link(self):
        title, body = rb.build_spoilers(
            "Todd exposes a secret on Sarah's birthday",
            "https://x/emmerdale-insider/spoilers/next-week-roundup/",
            datetime(2026, 5, 29, 18, 0, tzinfo=UK),
        )
        self.assertIn("Spoilers & Rumours", title)
        # A real, rendered spoiler cover (not inside backticks).
        self.assertIn(">!", body)
        self.assertIn("!<", body)
        self.assertIn("Todd exposes a secret", body)
        self.assertIn("next-week-roundup", body)

    def test_spoilers_long_headline_falls_back_to_generic_cover(self):
        long_headline = " ".join(["word"] * 30)
        _, body = rb.build_spoilers(long_headline, "https://x/s/", datetime(2026, 5, 29, 18, 0, tzinfo=UK))
        self.assertIn(">!", body)
        self.assertNotIn(long_headline, body)  # not reproduced verbatim

    def test_markers_distinct(self):
        self.assertEqual(rb.episode_marker("2026-05-29"), "emmerbot:2026-05-29:episode")
        self.assertEqual(rb.spoiler_marker("next-week-roundup"), "emmerbot:spoilers:next-week-roundup")
        self.assertNotEqual(rb.episode_marker("2026-05-29"), rb.spoiler_marker("x"))


@unittest.skipUnless(os.environ.get("LIVE_TESTS") == "1", "set LIVE_TESTS=1 to run live network checks")
class TestLive(unittest.TestCase):
    def test_tvmaze_returns_episodes(self):
        eps = rb.fetch_episodes()
        self.assertGreater(len(eps), 100)
        self.assertTrue(any(e.season >= 50 for e in eps))

    def test_spoiler_index_parses_recent_dates(self):
        resp = rb._http_get(rb.SPOILER_INDEX_URL)
        self.assertEqual(resp.status_code, 200, f"index HTTP {resp.status_code}")
        recs = rb._index_records(resp.text, date.today())
        dated = [r["date"] for r in recs if r["date"]]
        self.assertTrue(dated, "no dated entries parsed -- layout may have changed")
        self.assertTrue([d for d in dated if abs((d - date.today()).days) <= 14])

    def test_forward_and_today_detection_run_cleanly(self):
        resp = rb._http_get(rb.SPOILER_INDEX_URL)
        fwd_url, fwd_head = rb.get_forward_spoiler(resp.text, date.today())
        print(f"\n[LIVE] forward spoiler -> {rb.slug_from_url(fwd_url) if fwd_url else None}")
        state, _, reason = rb.detect_robron(date.today())
        print(f"[LIVE] today Robron detection -> state={state} reason={reason!r}")
        self.assertIn(state, (True, False, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
