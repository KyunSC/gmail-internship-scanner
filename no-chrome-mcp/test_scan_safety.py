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


TOWER_TITLE = 'Stagiaire en développement de logiciels (été 2027) / Software Developer Intern (Summer 2027)'


def glassdoor(id, prev_tail, listing, date='Tue, 22 Sep 2026 10:00:00 +0000', subject='New jobs in Montreal, QC'):
    """Glassdoor digest: the company chip sits at the end of the previous chunk."""
    return dict(id=id, subject=subject, date=date, **{'from': 'Glassdoor Jobs <noreply@glassdoor.com>'},
                body=f'{prev_tail} ★ {listing} ★ Backend Developer Toronto $80K 2d')


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
             patch.object(compare,'get_gmail_service'), patch.object(compare,'fetch_aggregator_emails',return_value=emails), \
             patch.object(compare,'load_scan_cache',return_value={}), \
             patch.object(compare,'rule_based_filter',return_value=([emails[1]], [emails[0],emails[2]])), \
             patch.object(compare,'analyze_with_ollama',return_value=scanner.AnalysisResults([], {'a','b'}, {'c'})), \
             patch.object(compare,'save_scan_cache',return_value={}), patch.object(compare,'clean_inbox',return_value=[]) as clean:
            compare.main()
            kw = clean.call_args.kwargs
            self.assertTrue(kw['apply'])
            self.assertEqual(kw['evaluated_ids'] - kw['keep_ids'], {'a'})
            self.assertEqual(kw['listing_keys'], {})

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

    def test_glassdoor_listing_key_takes_company_from_previous_chunk(self):
        e = glassdoor('a', 'Your recently viewed jobs Tower Research Capital 4.1',
                      f'{TOWER_TITLE} Montreal Jobs you might like Randstad 3.6')
        keys = scanner.qualifying_listing_keys(e['from'], e['body'], e['subject'])
        self.assertEqual(keys, ['glassdoor.com|tower research capital|stagiaire en developpement '
                                'de logiciels ete 2027 software developer intern summer 2027'])
        e2 = glassdoor('b', 'Software Developer QA I (Co-op) Montreal $44K - $185K ( Employer Est. ) '
                            'Just posted Tower Research Capital 4.1', f'{TOWER_TITLE} Montreal 5d CAE 3.9')
        self.assertEqual(scanner.qualifying_listing_keys(e2['from'], e2['body'], e2['subject']), keys)

    def test_same_title_different_company_gives_different_keys(self):
        listing = 'Software Developer Intern (Summer 2027) Montreal 2d'
        cae = glassdoor('a', 'Data Analyst Toronto Easy Apply 6d CAE 3.9', listing)
        tower = glassdoor('b', 'Data Analyst Toronto Easy Apply 6d Tower Research Capital 4.1', listing)
        k1 = scanner.qualifying_listing_keys(cae['from'], cae['body'], cae['subject'])
        k2 = scanner.qualifying_listing_keys(tower['from'], tower['body'], tower['subject'])
        self.assertTrue(k1 and k2)
        self.assertNotEqual(k1, k2)

    def test_unreliable_listing_keys_are_none(self):
        sender = 'noreply@glassdoor.com'
        first_chunk = 'Software Developer Intern (Summer 2027) Montreal 5d CAE 3.9 ★ Backend Developer Toronto 2d'
        no_lead = 'Backend Developer Toronto Tower Research Capital 4.1 ★ Software Developer Intern (Summer 2027) Montreal 2d'
        no_rating = 'Backend Developer Toronto 6d Tower Research Capital ★ Software Developer Intern (Summer 2027) Montreal 2d'
        term_after_location = ('Backend Developer Toronto 6d CAE 3.9 ★ '
                               'Software Developer Intern - Montreal - Summer 2027 - Backend 2d')
        for body in (first_chunk, no_lead, no_rating, term_after_location):
            with self.subTest(body=body):
                self.assertIsNone(scanner.qualifying_listing_keys(sender, body))
        self.assertIsNone(scanner.qualifying_listing_keys('friend@example.com', first_chunk))
        self.assertEqual(scanner.qualifying_listing_keys(sender, 'Backend Developer Toronto 6d CAE 3.9 ★ Chef Montreal'), [])

    def test_dedupe_keeps_newest_copy_of_each_listing(self):
        dates = {'old': 'Mon, 21 Sep 2026 10:00:00 +0000', 'mid': 'Tue, 22 Sep 2026 10:00:00 +0000 (UTC)',
                 'new': 'Thu, 24 Sep 2026 11:45:51 +0000 (UTC)', 'mixed': 'Sun, 20 Sep 2026 10:00:00 +0000',
                 'baddate': 'not a date', 'nokeys': 'Sat, 19 Sep 2026 10:00:00 +0000',
                 'empty': 'Sat, 19 Sep 2026 10:00:00 +0000'}
        keys = {'old': ['A'], 'mid': ['A'], 'new': ['A'], 'mixed': ['A', 'B'],
                'baddate': ['A'], 'nokeys': None, 'empty': []}
        dupes = scanner.dedupe_kept_ids(dates, keys, dates)
        self.assertEqual(dupes, {'old': 'new', 'mid': 'new'})
        tie = scanner.dedupe_kept_ids(['x', 'y'], {'x': ['A'], 'y': ['A']},
                                      {'x': dates['new'], 'y': dates['new']})
        self.assertEqual(tie, {'x': 'y'})

    def test_clean_inbox_marks_older_duplicates_only_when_safe(self):
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        ids = ('new', 'old', 'safety', 'unevaluated', 'unique')
        messages.list.return_value.execute.return_value = {'messages': [{'id': x} for x in ids]}
        cache = {x: dict(email(x), date=f'Mon, {21 - i} Sep 2026 10:00:00 +0000') for i, x in enumerate(ids)}
        cache['safety']['subject'] = 'Software Developer Intern Summer 2027 at Tower'
        keys = {'new': ['A'], 'old': ['A'], 'safety': ['A'], 'unevaluated': ['A'], 'unique': ['A', 'B']}
        marked = scanner.clean_inbox(service, apply=True, email_cache=cache, keep_ids=set(ids),
                                     evaluated_ids=set(ids) - {'unevaluated'}, listing_keys=keys)
        self.assertEqual([m['id'] for m in marked], ['old'])
        self.assertEqual(marked[0]['newer_copy'], 'Daily jobs')
        messages.batchModify.assert_called_once_with(userId='me', body={'ids': ['old'], 'removeLabelIds': ['UNREAD']})
        messages.batchModify.reset_mock()
        self.assertEqual(scanner.clean_inbox(service, apply=True, email_cache=cache, keep_ids=set(ids),
                                             evaluated_ids=set(ids)), [])
        messages.batchModify.assert_not_called()

    def test_scan_cache_stores_merges_and_prunes_listing_keys(self):
        tower = glassdoor('a', 'Your recently viewed jobs Tower Research Capital 4.1', f'{TOWER_TITLE} Montreal 5d CAE 3.9')
        other = glassdoor('b', 'Chef Toronto 6d', f'{TOWER_TITLE} Montreal 5d CAE 3.9')
        with tempfile.TemporaryDirectory() as tmp, patch.object(scanner, 'CACHE_PATH', Path(tmp)/'cache.json'):
            cache = scanner.save_scan_cache([tower, other], [tower, other])
            self.assertEqual(len(cache['listing_keys']['a']), 1)
            self.assertIn('tower research capital', cache['listing_keys']['a'][0])
            self.assertIsNone(cache['listing_keys']['b'])
            cache = scanner.save_scan_cache([email('c')], [email('c')])
            self.assertEqual(set(cache['listing_keys']), {'a', 'b', 'c'})
            cache = scanner.save_scan_cache([tower], scanner.AnalysisResults([], {'a'}))
            self.assertEqual(set(cache['listing_keys']), {'b', 'c'})
            with patch.object(scanner, 'SEEN_CACHE_MAX', 1):
                cache = scanner.save_scan_cache([email('d')], [email('d')])
            self.assertEqual(set(cache['listing_keys']), {'d'})
            self.assertEqual(json.loads((Path(tmp)/'cache.json').read_text())['listing_keys'], cache['listing_keys'])

if __name__ == '__main__':
    unittest.main()
