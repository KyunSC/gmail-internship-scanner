"""Regression checks for Intern Insider cleanup (no Gmail or Ollama calls)."""

import contextlib
import io
import unittest
from unittest.mock import MagicMock

from scanner import CLEAN_INBOX_SENDERS, clean_inbox


class InternInsiderCleanupTests(unittest.TestCase):
    def test_rejected_alert_is_marked_and_surfaced_alert_stays_unread(self):
        self.assertIn("alerts@interninsider.me", CLEAN_INBOX_SENDERS)
        self.assertNotIn("interninsider.me", CLEAN_INBOX_SENDERS)
        sender = "Intern Insider <alerts@interninsider.me>"
        cache = {
            key: {"id": key, "from": sender, "subject": '1 new match for "Summer 2027"',
                  "date": key}
            for key in ("retained", "rejected")
        }
        for use_ids in (False, True):
            with self.subTest(use_ids=use_ids):
                service = MagicMock()
                messages = service.users.return_value.messages.return_value
                messages.list.return_value.execute.return_value = {
                    "messages": [{"id": key} for key in cache]
                }
                with contextlib.redirect_stdout(io.StringIO()):
                    marked = clean_inbox(
                        service, results=[cache["retained"]], apply=True,
                        email_cache=cache,
                        keep_ids={"retained"} if use_ids else None,
                    )
                self.assertIn("from:alerts@interninsider.me", messages.list.call_args.kwargs["q"])
                self.assertEqual([m["id"] for m in marked], ["rejected"])
                messages.batchModify.assert_called_once_with(
                    userId="me", body={"ids": ["rejected"], "removeLabelIds": ["UNREAD"]}
                )


if __name__ == "__main__":
    unittest.main()
