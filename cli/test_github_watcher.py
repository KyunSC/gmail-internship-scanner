"""Checks for github_watcher: schema normalization, README-table parsing, the
listing filter, cross-repo dedup, and the seen-set / ETag state across runs.
No network — requests.get is faked and SOURCES is replaced with test sources.

    venv/bin/python -m unittest test_github_watcher -v
"""

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import github_watcher as gw

NOW = time.time()
HOUR = 3600
SIMPLIFY = {"name": "simplify", "url": "https://raw.example/simplify.json",
            "format": "json", "default_year": "2027"}
VANSH = {"name": "vansh", "url": "https://raw.example/vansh.json",
         "format": "json", "default_year": "2027"}
NEGAR = {"name": "negar", "url": "https://raw.example/README.md", "format": "markdown"}
TEST_SOURCES = [SIMPLIFY, VANSH, NEGAR]


def simplify_row(title, company, locations, *, terms=("Summer 2027",),
                 category="Software", active=True, age=HOUR, url=None):
    return {"id": f"s-{company}-{title}", "company_name": company, "title": title,
            "locations": list(locations), "terms": list(terms), "category": category,
            "active": active, "is_visible": True, "date_posted": int(NOW - age),
            "url": url or f"https://jobs.example/{company}/{title.replace(' ', '-')}"}


def vansh_row(title, company, locations, *, season="Summer", age=HOUR, url=None):
    return {"id": f"v-{company}-{title}", "company_name": company, "title": title,
            "locations": list(locations), "season": season, "active": True,
            "is_visible": True, "date_posted": int(NOW - age),
            "url": url or f"https://jobs.example/{company}/{title.replace(' ', '-')}"}


def readme_date(age=HOUR):
    return datetime.fromtimestamp(NOW - age, timezone.utc).strftime("%b %-d, %Y")


def badge(url):
    return f"[![Apply](https://img.shields.io/badge/-Apply-blue?style=for-the-badge)]({url})"


SIMPLIFY_ROWS = [
    simplify_row("Software Engineer Intern", "Acme", ["Toronto, ON, Canada"]),        # keep
    simplify_row("Backend Developer Intern", "Beta", ["Remote in Canada"]),           # keep
    simplify_row("Firmware Engineer Intern", "Delta", ["Markham, ON, Canada"],
                 category="Hardware"),                                                # keep
    simplify_row("Software Engineer Intern", "Usco", ["Remote in USA"]),              # --anywhere only
    simplify_row("Software Engineer Intern", "Oldterm", ["Toronto, ON, Canada"],
                 terms=("Spring 2026",)),                                             # keep: not Winter/Fall
    simplify_row("Software Engineer Intern", "Closed", ["Toronto, ON, Canada"], active=False),
    simplify_row("QA Automation Intern", "Testco", ["Toronto, ON, Canada"]),
    simplify_row("Analog Design Intern", "Chipco", ["Ottawa, ON, Canada"], category="Hardware"),
    simplify_row("Reporting Analyst Intern", "Insureco", ["Toronto, ON, Canada"],
                 category="AI/ML/Data"),
    simplify_row("Software Engineer Intern", "Stale", ["Toronto, ON, Canada"], age=60 * 86400),
    simplify_row("Software Developer Intern", "Nadate", ["Toronto, ON, Canada"],
                 terms=("N/A",)),                                                     # keep: termless
]
VANSH_ROWS = [
    vansh_row("Software Engineer Intern", "Gamma", ["Montreal, Canada"]),             # keep
    vansh_row("Software Engineering Intern", "Acme Inc", ["Toronto, ON"]),            # dup of Acme by name
    vansh_row("Software Engineer Intern", "Fallco", ["Toronto, ON"], season="Fall"),
    # Same posting as Simplify's Beta, company spelled differently: dup by URL.
    vansh_row("Backend Developer Intern", "Beta Technologies Inc",
              ["Remote in Canada"], url=SIMPLIFY_ROWS[1]["url"] + "?utm_source=vansh"),
]
NEGAR_README = f"""# Canadian Tech Internships

| Company | Role | Location | Apply | Date Posted |
|--------|------|----------|:-----:|--------------|
| Intelcom \\| Dragonfly | Software Developer Intern (Summer 2027) | Montreal, QC / Remote, Canada | {badge("https://jobs.example/intelcom/1")} | {readme_date()} |
| ↳ | Data Engineer Intern (Winter 2027) | Montreal, QC | {badge("https://jobs.example/intelcom/2")} | {readme_date()} |
| ↳ | Back-end Developer Intern | Montreal, QC | {badge("https://jobs.example/intelcom/3")} | {readme_date()} |
| Nokia | Software Tools Development Co-op Intern (Summer 2027) | Ottawa, ON | Closed🔒 | {readme_date()} |

Contributing text after the table.
"""
# Only an explicit Winter/Fall term drops a posting: termless Nadate and
# Spring-2026 Oldterm stay, Fallco goes.
EXPECTED = {"Acme", "Beta", "Delta", "Gamma", "Intelcom | Dragonfly", "Nadate", "Oldterm"}
EXPECTED_POSTINGS = len(EXPECTED) + 1  # Intelcom: the Summer 2027 row and the termless one


def response(status, data=None, etag=None):
    resp = MagicMock(status_code=status, headers={"ETag": etag} if etag else {})
    resp.json.return_value = data
    resp.text = data if isinstance(data, str) else ""
    resp.raise_for_status.side_effect = (
        requests.HTTPError(f"{status}") if status >= 400 else None)
    return resp


class FakeGitHub:
    """Serves each source's rows with a fixed ETag, and 304 when it's echoed."""

    def __init__(self, rows=None, etags=None, fail=()):
        self.rows = rows or {SIMPLIFY["url"]: SIMPLIFY_ROWS, VANSH["url"]: VANSH_ROWS,
                             NEGAR["url"]: NEGAR_README}
        self.etags = etags or {s["url"]: f'"e-{s["name"]}"' for s in TEST_SOURCES}
        self.fail = set(fail)
        self.sent = []

    def __call__(self, url, headers=None, timeout=None):
        sent = (headers or {}).get("If-None-Match")
        self.sent.append((url, sent))
        if url in self.fail:
            raise requests.ConnectionError("network down")
        if sent and sent == self.etags[url]:
            return response(304)
        return response(200, self.rows[url], self.etags[url])


def all_listings():
    rows = ([(r, SIMPLIFY) for r in SIMPLIFY_ROWS] + [(r, VANSH) for r in VANSH_ROWS]
            + [(r, NEGAR) for r in gw.parse_markdown_listings(NEGAR_README)])
    return [gw.normalize_listing(r, s) for r, s in rows]


class NormalizeAndFilterTests(unittest.TestCase):
    def test_simplify_schema(self):
        l = gw.normalize_listing(SIMPLIFY_ROWS[0], SIMPLIFY)
        self.assertEqual(l["terms"], ["Summer 2027"])
        self.assertEqual((l["company"], l["category"], l["source"]), ("Acme", "Software", "simplify"))

    def test_vansh_season_gets_repo_year(self):
        l = gw.normalize_listing(VANSH_ROWS[0], VANSH)
        self.assertEqual(l["terms"], ["Summer 2027"])
        self.assertIsNone(l["category"])

    def wanted(self, **kwargs):
        # The filter alone doesn't dedupe, so vansh's copies of Acme and Beta
        # (spelled differently) pass too; watch() folds them into the originals.
        return ({l["company"] for l in all_listings() if gw.is_wanted(l, 30, now=NOW, **kwargs)}
                - {"Acme Inc", "Beta Technologies Inc"})

    def test_filter_keeps_canadian_software_summer_2027(self):
        self.assertEqual(self.wanted(), EXPECTED)

    def test_anywhere_drops_only_the_location_rule(self):
        self.assertEqual(self.wanted(anywhere=True), EXPECTED | {"Usco"})

    def test_only_explicit_winter_or_fall_is_dropped(self):
        self.assertNotIn("Fallco", self.wanted())
        titles = {l["title"] for l in all_listings() if gw.is_wanted(l, 30, now=NOW)}
        self.assertIn("Back-end Developer Intern", titles)
        self.assertNotIn("Data Engineer Intern (Winter 2027)", titles)


class MarkdownParseTests(unittest.TestCase):
    def setUp(self):
        self.rows = gw.parse_markdown_listings(NEGAR_README)

    def test_rows_and_repeated_company(self):
        self.assertEqual(len(self.rows), 4)
        self.assertEqual({r["company_name"] for r in self.rows[:3]}, {"Intelcom | Dragonfly"})
        self.assertEqual(self.rows[3]["company_name"], "Nokia")

    def test_apply_link_skips_badge_image(self):
        self.assertEqual(self.rows[0]["url"], "https://jobs.example/intelcom/1")

    def test_closed_row_is_inactive(self):
        self.assertTrue(self.rows[0]["active"])
        self.assertFalse(self.rows[3]["active"])

    def test_locations_split_and_date_parsed(self):
        self.assertEqual(self.rows[0]["locations"], ["Montreal, QC", "Remote, Canada"])
        self.assertLess(abs(self.rows[0]["date_posted"] - (NOW - HOUR)), 86400)

    def test_terms_come_from_title_when_stated(self):
        self.assertTrue(self.rows[0]["terms"])
        self.assertEqual(self.rows[2]["terms"], [])

    def test_no_table_is_an_error(self):
        fake = FakeGitHub(rows={NEGAR["url"]: "# Nothing here"})
        with patch.object(gw.requests, "get", side_effect=fake):
            with self.assertRaises(ValueError):
                gw.fetch_source(NEGAR, None)


class WatchStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "seen.json"
        for name, value in (("STATE_PATH", self.state_path), ("SOURCES", TEST_SOURCES)):
            patcher = patch.object(gw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_watch(self, fake, **kwargs):
        with patch.object(gw.requests, "get", side_effect=fake):
            return gw.watch(**kwargs)

    def test_first_run_shows_all_then_304_shows_none(self):
        fake = FakeGitHub()
        shown, statuses = self.run_watch(fake)
        self.assertEqual({l["company"] for l in shown}, EXPECTED)
        self.assertEqual(len(shown), EXPECTED_POSTINGS, "cross-repo duplicate shown twice")
        self.assertEqual(set(statuses.values()), {"updated"})

        fake.sent.clear()
        shown, statuses = self.run_watch(fake)
        self.assertEqual(shown, [])
        self.assertEqual(set(statuses.values()), {"unchanged"})
        self.assertEqual({s for _, s in fake.sent}, {f'"e-{s["name"]}"' for s in TEST_SOURCES})

    def test_only_new_postings_after_repo_update(self):
        self.run_watch(FakeGitHub())
        new_row = simplify_row("Software Developer Intern", "Newco", ["Vancouver, BC, Canada"])
        rows = {SIMPLIFY["url"]: SIMPLIFY_ROWS + [new_row], VANSH["url"]: VANSH_ROWS,
                NEGAR["url"]: NEGAR_README}
        etags = {s["url"]: f'"e-{s["name"]}"' for s in TEST_SOURCES}
        etags[SIMPLIFY["url"]] = '"e-simplify-2"'
        shown, _ = self.run_watch(FakeGitHub(rows=rows, etags=etags))
        self.assertEqual([l["company"] for l in shown], ["Newco"])

    def test_same_posting_in_another_repo_is_not_new_later(self):
        # Seen once via Simplify; a later repo listing the same URL under a
        # different company name must not resurface it.
        rows = {SIMPLIFY["url"]: SIMPLIFY_ROWS, VANSH["url"]: [], NEGAR["url"]: NEGAR_README}
        self.run_watch(FakeGitHub(rows=rows))
        etags = {s["url"]: f'"e-{s["name"]}"' for s in TEST_SOURCES}
        etags[VANSH["url"]] = '"e-vansh-2"'
        shown, _ = self.run_watch(FakeGitHub(etags=etags))
        self.assertEqual([l["company"] for l in shown], ["Gamma"])

    def test_show_all_ignores_seen_and_etag(self):
        self.run_watch(FakeGitHub())
        fake = FakeGitHub()
        shown, _ = self.run_watch(fake, show_all=True)
        self.assertEqual({l["company"] for l in shown}, EXPECTED)
        self.assertEqual({s for _, s in fake.sent}, {None})

    def test_no_mark_writes_nothing(self):
        shown, _ = self.run_watch(FakeGitHub(), mark=False)
        self.assertEqual(len(shown), EXPECTED_POSTINGS)
        self.assertFalse(self.state_path.exists())

    def test_failed_source_keeps_no_etag(self):
        fake = FakeGitHub(fail={VANSH["url"]})
        shown, statuses = self.run_watch(fake)
        self.assertTrue(statuses["vansh"].startswith("error"))
        self.assertEqual({l["company"] for l in shown}, EXPECTED - {"Gamma"})
        state = gw.load_state()
        self.assertIn("simplify", state["sources"])
        self.assertNotIn("vansh", state["sources"])

        # Next run retries vansh from scratch and surfaces what it missed.
        fake = FakeGitHub()
        shown, _ = self.run_watch(fake)
        self.assertEqual([l["company"] for l in shown], ["Gamma"])
        self.assertIn((VANSH["url"], None), fake.sent)

    def test_changed_filters_refetch_instead_of_trusting_304(self):
        self.run_watch(FakeGitHub())
        for kwargs, extra in (({"anywhere": True}, {"Usco"}),):
            fake = FakeGitHub()
            shown, _ = self.run_watch(fake, **kwargs)
            self.assertEqual({s for _, s in fake.sent}, {None}, kwargs)
            self.assertEqual({l["company"] for l in shown} - EXPECTED, extra - EXPECTED, kwargs)


if __name__ == "__main__":
    unittest.main()
