import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import compare_models as trial
import scanner


class TrialTests(unittest.TestCase):
    def test_exact_availability(self):
        for requested, installed, available in [
            ('qwen', 'qwen:latest', True), ('qwen:latest', 'qwen', True),
            ('qwen:9b', 'qwen:27b', False),
            (trial.CANDIDATE, trial.CANDIDATE.replace('IQ4_XS', 'Q4_K_M'), False),
            (trial.CANDIDATE, trial.CANDIDATE, True),
            ('qwen:9b', 'other/qwen:9b', False),
        ]:
            with self.subTest(requested=requested, installed=installed), patch.object(scanner, 'OLLAMA_MODEL', requested), patch.object(scanner.requests, 'get') as get, contextlib.redirect_stdout(io.StringIO()):
                get.return_value.json.return_value = {'models': [{'name': installed}]}
                if available:
                    self.assertTrue(scanner.check_ollama())
                else:
                    with self.assertRaises(SystemExit):
                        scanner.check_ollama()

    def test_identical_inputs_failures_and_unload_order(self):
        emails = [dict(trial.SYNTHETIC, id=x) for x in ('a', 'b', 'c')]
        events = []
        def analyze(sample, **kwargs):
            self.assertEqual(sample, emails)
            self.assertEqual(kwargs, {'prefilter': False})
            model = scanner.OLLAMA_MODEL
            events.append(('analyze', model))
            sample[0]['body'] = 'mutation must not affect next model'
            return (scanner.AnalysisResults([{'id': 'a'}], {'a', 'b'}, {'c'}) if model == 'base'
                    else scanner.AnalysisResults([], {'a', 'b', 'c'}))
        original = scanner.OLLAMA_MODEL
        with patch.object(scanner, 'analyze_with_ollama', side_effect=analyze), patch.object(trial, 'memory_snapshot', return_value={}), patch.object(trial, 'unload', side_effect=lambda m: events.append(('unload', m))):
            report = trial.compare(emails, 'base', 'candidate')
        self.assertEqual(events, [('analyze', 'base'), ('unload', 'base'), ('analyze', 'candidate'), ('unload', 'candidate')])
        self.assertEqual(scanner.OLLAMA_MODEL, original)
        self.assertEqual(report['runs'][0]['failed_email_ids'], ['c'])
        self.assertEqual([d['id'] for d in report['disagreements']], ['a'])

    def test_main_readonly_and_no_caches(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            stack.enter_context(patch.object(trial, 'TRIAL_DIR', root))
            stack.enter_context(patch.object(scanner, 'check_ollama'))
            stack.enter_context(patch.object(trial, 'unload'))
            stack.enter_context(patch.object(trial, 'memory_snapshot', return_value={}))
            auth = stack.enter_context(patch.object(scanner, 'get_gmail_service'))
            fetch = stack.enter_context(patch.object(scanner, 'fetch_aggregator_emails', return_value=[trial.SYNTHETIC]))
            stack.enter_context(patch.object(scanner, 'analyze_with_ollama', return_value=scanner.AnalysisResults([], {'synthetic'})))
            for name in ('save_scan_cache', 'load_scan_cache', 'clear_scan_cache', 'clean_inbox'):
                stack.enter_context(patch.object(scanner, name, side_effect=AssertionError(name)))
            for name in ('CACHE_PATH', 'RESULTS_PATH'):
                path = root / name
                path.write_text('unchanged')
                stack.enter_context(patch.object(scanner, name, path))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(trial.main(['--output', str(root / 'trial.json')]), 0)
            auth.assert_called_once_with(write_access=False)
            fetch.assert_called_once_with(auth.return_value, days=30, unread_only=True, max_results=20)
            auth.return_value.users.assert_not_called()
            for name in ('CACHE_PATH', 'RESULTS_PATH'):
                self.assertEqual((root / name).read_text(), 'unchanged')
            self.assertEqual(json.loads((root / 'trial.json').read_text())['status'], 'completed')

    def test_failed_synthetic_never_accesses_gmail(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(trial, 'TRIAL_DIR', Path(tmp)), patch.object(scanner, 'check_ollama'), patch.object(trial, 'unload'), patch.object(trial, 'evaluate', return_value={'failed_email_ids': ['synthetic']}), patch.object(scanner, 'get_gmail_service') as auth, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trial.main([]), 1)
            auth.assert_not_called()


if __name__ == '__main__':
    unittest.main()
