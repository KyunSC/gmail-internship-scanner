"""Offline classification, persistence, query and cleanup regressions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import requests
import scanner
import compare


def email(id):
    return dict(id=id, subject='Daily jobs', date=id, **{'from': 'jobs-noreply@linkedin.com'},
                body='Software Engineering Intern Summer 2027 Acme · Montreal, QC')


class ScanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def analyze(self, responses):
        def response(value):
            if isinstance(value, Exception):
                raise value
            mock = MagicMock()
            mock.json.return_value = {'response': value}
            return mock
        with patch.object(scanner.requests, 'post', side_effect=lambda *a, **k: response(next(responses))):
            return scanner.analyze_with_ollama([email('a'), email('b')], prefilter=False)

    def test_failures_empty_and_mixed(self):
        for bad in ('not json', '{}', '{"results":[42]}', '{"results":[{}]}', requests.Timeout()):
            with self.subTest(bad=bad):
                out = self.analyze(iter([bad, bad, '{"results":[]}', '{"results":[]}']))
                self.assertEqual(out, [])
                self.assertEqual(out.failed_ids, {'a'})
                self.assertEqual(out.successful_ids, {'b'})
        out = self.analyze(iter(['{"results":[]}', requests.Timeout(), requests.Timeout(), requests.Timeout()]))
        self.assertEqual(out.failed_ids, {'a', 'b'})

    def test_rescan_replaces_success_preserves_failure_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(scanner, 'CACHE_PATH', Path(tmp)/'cache.json'):
            scanner.save_scan_cache([email('a'), email('b')], [email('a'), email('b')])
            cache = scanner.save_scan_cache([email('a'), email('b')], scanner.AnalysisResults([], {'a'}, {'b'}))
            self.assertEqual(cache['kept_ids'], ['b'])
            self.assertEqual(cache['failed_ids'], ['b'])
            cache = scanner.save_scan_cache([email('b')], scanner.AnalysisResults([], {'b'}))
            self.assertEqual(cache['kept_ids'], [])
            self.assertEqual(cache['failed_ids'], [])

    def test_paginated_cleanup_only_successful_current_scan(self):
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.side_effect = [
            {'messages': [{'id': x} for x in ('a', 'failed', 'capped')], 'nextPageToken':'next'},
            {'messages': [{'id':'fetch-failed'}, {'id':'outside-keyword'}]},
        ]
        marked = scanner.clean_inbox(service, apply=True, email_cache={'a':email('a')},
                                     keep_ids=set(), evaluated_ids={'a'})
        self.assertEqual([m['id'] for m in marked], ['a'])
        messages.get.assert_not_called()
        messages.batchModify.assert_called_once_with(userId='me', body={'ids':['a'], 'removeLabelIds':['UNREAD']})

    def test_compare_apply_requires_successful_agreement(self):
        emails = [email(x) for x in ('a','b','c')]
        with patch('sys.argv', ['compare.py','--apply']), \
             patch.object(compare,'get_gmail_service'), patch.object(compare,'run_gmail_search',return_value=emails), \
             patch.object(compare,'load_scan_cache',return_value={}), \
             patch.object(compare,'rule_based_filter',return_value=([emails[1]], [emails[0],emails[2]])), \
             patch.object(compare,'analyze_with_ollama',return_value=scanner.AnalysisResults([], {'a','b'}, {'c'})), \
             patch.object(compare,'save_scan_cache',return_value={}), patch.object(compare,'clean_inbox',return_value=[]) as clean:
            compare.main()
            kw = clean.call_args.kwargs
            self.assertTrue(kw['apply'])
            self.assertEqual(kw['evaluated_ids'] - kw['keep_ids'], {'a'})

    def test_fetch_cap_and_failed_body_never_enter_cleanup_scope(self):
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {
            'messages': [{'id':'a'}, {'id':'fetch-failed'}], 'nextPageToken':'capped'}
        def get_message(**kw):
            response = MagicMock()
            if kw['id'] == 'fetch-failed':
                response.execute.side_effect = RuntimeError('offline fixture failure')
            else:
                response.execute.return_value = {'payload':{'headers':[
                    {'name':'Subject','value':'Daily jobs'},
                    {'name':'From','value':'jobs-noreply@linkedin.com'}]}}
            return response
        messages.get.side_effect = get_message
        fetched = scanner.run_gmail_search(service, keyword='Acme', max_results=2)
        self.assertEqual([e['id'] for e in fetched], ['a'])
        self.assertEqual(messages.list.call_args.kwargs['maxResults'], 2)
        messages.list.return_value.execute.return_value = {
            'messages':[{'id':id} for id in ('a','fetch-failed','capped')]}
        results = scanner.rule_based_analyze(fetched)
        marked = scanner.clean_inbox(service, results, apply=True,
                                     email_cache={e['id']:e for e in fetched},
                                     evaluated_ids={e['id'] for e in fetched})
        self.assertEqual([e['id'] for e in marked], ['a'])

    def test_cached_cleanup_excludes_failures_even_with_broad_opt_in(self):
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {'messages':[{'id':'failed'}]}
        self.assertEqual(scanner.clean_inbox(service, apply=True, from_cache=True,
                                            failed_ids={'failed'}), [])
        messages.get.assert_not_called()
        messages.batchModify.assert_not_called()

    def test_keyword_cutoff_and_grouping(self):
        with patch.object(scanner,'search_emails',return_value=[]) as search:
            scanner.run_gmail_search(None, keyword='Acme OR Beta', days=7)
            query = search.call_args.args[1]
            self.assertTrue(query.startswith('is:unread (Acme OR Beta) (intern OR'))
            self.assertIn(' after:',query)

    def test_badge_free_digest_cannot_mix_jobs(self):
        e = email('a')
        e['body'] = 'Software Engineer Acme · Toronto, ON Finance Intern Summer 2027 Bank · Montreal, QC'
        self.assertEqual(scanner.rule_based_analyze([e]), [])
        self.assertEqual(len(scanner._split_aggregator_listings(e['from'],e['body'],e['subject'])), 2)

if __name__ == '__main__':
    unittest.main()
