"""Checks for plan_postings (per-posting skip/check, link matching, the plan)
and the verify-postings mark_read selection. No network or Gmail.

    venv/bin/python -m unittest test_plan_postings -v
"""

import importlib.util
import time
import unittest
from pathlib import Path

import plan_postings as pp

_spec = importlib.util.spec_from_file_location(
    "mark_read", Path(__file__).parent / ".claude/skills/verify-postings/mark_read.py")
mark_read = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mark_read)

WELLFOUND = "Wellfound <team@hi.wellfound.com>"
GLASSDOOR = "Glassdoor Jobs <noreply@glassdoor.com>"
LINKEDIN = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"


def wf_card(title, city="Montreal"):
    return (f"{title} Nimbus Labs / 11-50 Employees | In office, {city} | "
            f"0 years of exp | Internship Actively Hiring Learn More")


def gd_link(text, jl):
    return (text, f"https://www.glassdoor.ca/partner/jobListing.htm?jobListingId={jl}&utm_source=x")


class SkipReasonTests(unittest.TestCase):
    def test_reasons_in_order(self):
        cases = [
            ("Software Developer Nimbus Montreal", "not an internship"),
            ("Marketing Intern Nimbus Montreal", "not software/tech"),
            ("Software Developer Intern Nimbus Seattle, WA", "not in Canada"),
            ("Software QA Engineer Intern Nimbus Montreal", "QA/testing role"),
            ("Software Developer Intern (Winter 2027) Nimbus Montreal", "Winter/Fall term"),
            ("Software Developer Intern Nimbus Montreal", None),
            ("Software Developer Intern (Summer 2027) Nimbus Montreal", None),
        ]
        for chunk, reason in cases:
            with self.subTest(chunk=chunk):
                self.assertEqual(pp.skip_reason(chunk), reason)

    def test_non_aggregator_skips_location(self):
        self.assertIsNone(pp.skip_reason("Software Developer Intern Nimbus Seattle", aggregator=False))


class JobLinkTests(unittest.TestCase):
    def test_chrome_dropped_titles_kept(self):
        raw = [("Unsubscribe", "https://x.example/u"), ("Manage alerts", "https://x.example/m"),
               ("Help Desk Intern", "https://x.example/j1"), ("Help", "https://x.example/h"),
               ("Your job alert for Engineer Intern", "https://x.example/a")]
        self.assertEqual(pp.job_links(raw, WELLFOUND), [("Help Desk Intern", "https://x.example/j1")])

    def test_linkedin_only_job_pages_without_tracking(self):
        raw = [("Software Developer Intern", "https://www.linkedin.com/comm/jobs/view/123?trk=x"),
               ("Jobs for you", "https://www.linkedin.com/comm/jobs/search?x=1"),
               ("Messaging", "https://www.linkedin.com/comm/messaging/")]
        self.assertEqual(pp.job_links(raw, LINKEDIN),
                         [("Software Developer Intern", "https://www.linkedin.com/jobs/view/123/")])

    def test_glassdoor_only_listing_links(self):
        raw = [gd_link("Acme 4.1 ★ Software Intern Montreal", 1),
               ("Interview question of the week", "https://www.glassdoor.ca/Interview/q.htm")]
        self.assertEqual([t for t, _ in pp.job_links(raw, GLASSDOOR)], ["Acme 4.1 ★ Software Intern Montreal"])


class AssignLinkTests(unittest.TestCase):
    def test_containment_prefix_and_star(self):
        chunks = ["Software Developer Intern Acme · Montreal, QC",
                  "Safety Methodologies Intern/Co-op Waabi Toronto, Ontario, Canada",
                  "Backend Developer Intern Montreal 3d Beta 4.0"]
        links = [("Software Developer Intern", "u1"),
                 ("Safety Methodologies Intern/Co-op Waabi Toronto, Ontario, Canada just now", "u2"),
                 ("Acme 4.1 ★ Backend Developer Intern Montreal", "u3")]
        got = pp.assign_links(chunks, links)
        self.assertEqual({i: [u for _, u in v] for i, v in got.items()},
                         {0: ["u1"], 1: ["u2"], 2: ["u3"]})

    def test_repeated_title_goes_to_next_posting_in_order(self):
        chunks = ["Machine Learning Intern Acme · Montreal", "Machine Learning Intern Beta · Toronto"]
        links = [("Machine Learning Intern", "a"), ("Machine Learning Intern", "b")]
        got = pp.assign_links(chunks, links)
        self.assertEqual({i: [u for _, u in v] for i, v in got.items()}, {0: ["a"], 1: ["b"]})

    def test_short_cta_links_are_ignored(self):
        self.assertEqual(pp.assign_links(["Software Intern Montreal"], [("Learn More", "x")]), {})


class PlanEmailTests(unittest.TestCase):
    def test_wellfound_digest(self):
        body = " ".join([wf_card("Software Engineer Intern"),
                         wf_card("Software Engineer Intern (Winter 2027)"),
                         wf_card("Software Engineer Intern", city="Seattle")])
        links = [("Software Engineer Intern", "https://wellfound.com/jobs/1"),
                 ("Learn More", "https://wellfound.com/jobs/1"),
                 ("Software Engineer Intern (Winter 2027)", "https://wellfound.com/jobs/2")]
        e = pp.plan_email("m1", WELLFOUND, "New jobs", "d", body, links)
        self.assertEqual([p["skip"] for p in e["postings"]], [None, "Winter/Fall term", "not in Canada"])
        self.assertEqual(e["postings"][0]["links"][0]["url"], "https://wellfound.com/jobs/1")

    def test_glassdoor_links_are_the_postings(self):
        # The second card has no rating, so a ★ split would merge it into the first.
        body = ("Acme 4.1 ★ Senior Developer Montreal $90K 1d Beta Stage developer intern Montreal 3d")
        links = [gd_link("Acme 4.1 ★ Senior Developer Montreal $90K 1d", 11),
                 gd_link("Beta Stage developer intern Montreal 3d", 22)]
        e = pp.plan_email("m1", GLASSDOOR, "Jobs for you", "d", body, links)
        self.assertEqual([(p["skip"], p["key"]) for p in e["postings"]],
                         [("not an internship", "glassdoor:11"), (None, "glassdoor:22")])

    def test_title_link_overrides_borrowed_intern_keyword(self):
        chunk = "Summer Intern Manage alerts C-GE-900 Développeur Unreal Acme · Montreal, QC"
        e = pp.plan_email("m1", "x <jobs@example.com>", "s", "d", chunk,
                          [("C-GE-900 Développeur Unreal", "https://example.com/j/1")])
        self.assertEqual(e["postings"][0]["skip"], "not an internship (title)")

    def test_account_nudges_are_skipped(self):
        e = pp.plan_email("m1", "Indeed <no-reply@indeed.com>", "Confirm your interest", "d",
                          "Software Developer Intern Montreal", [])
        self.assertEqual(e["postings"][0]["skip"], "not a job posting (account nudge)")


def planned(msg_id, subject, postings):
    return {"id": msg_id, "from": WELLFOUND, "subject": subject, "date": "d",
            "postings": [{"text": t, "skip": s, "links": [], "key": k} for t, s, k in postings]}


class BuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = pp.build_plan([
            planned("a", "New jobs", [("A", None, "k1"), ("B", "not in Canada", "k2")]),
            planned("b", "New jobs", [("A again", None, "k1"), ("C", None, "k3")]),
            planned("c", "New jobs", [("D", "Winter/Fall term", "k4")]),
            planned("d", "Software Developer Intern (Summer 2027)", [("E", "not in Canada", "k5")]),
        ], days=30, unread_only=True)

    def test_postings_deduped_across_emails(self):
        self.assertEqual([(p["id"], p["emails"]) for p in self.plan["postings"]],
                         [("p1", ["a", "b"]), ("p2", ["b"])])
        self.assertEqual({e["id"]: e["check"] for e in self.plan["emails"]},
                         {"a": ["p1"], "b": ["p1", "p2"], "c": [], "d": []})

    def test_all_skipped_and_subject_hold(self):
        by_id = {e["id"]: e for e in self.plan["emails"]}
        self.assertTrue(by_id["c"]["all_skipped"] and not by_id["c"]["hold"])
        self.assertTrue(by_id["d"]["hold"])

    def test_mark_read_selection(self):
        to_mark, kept = mark_read.emails_to_mark(self.plan, set())
        self.assertEqual([e["id"] for e in to_mark], ["c"])
        to_mark, kept = mark_read.emails_to_mark(self.plan, {"p1"})
        self.assertEqual([e["id"] for e in to_mark], ["a", "c"])
        to_mark, kept = mark_read.emails_to_mark(self.plan, {"p1", "p2"})
        self.assertEqual([e["id"] for e in to_mark], ["a", "b", "c"])
        self.assertEqual([e["id"] for e, _ in kept], ["d"])

    def test_mark_read_refuses_unknown_ids_and_other_senders(self):
        with self.assertRaises(ValueError):
            mark_read.emails_to_mark(self.plan, {"p99"})
        self.plan["emails"][2]["from"] = "Friend <friend@example.com>"
        to_mark, _ = mark_read.emails_to_mark(self.plan, set())
        self.assertEqual(to_mark, [])

    def test_plan_is_timestamped(self):
        self.assertLessEqual(abs(self.plan["generated_at"] - time.time()), 5)


if __name__ == "__main__":
    unittest.main()
