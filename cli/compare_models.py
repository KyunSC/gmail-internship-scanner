"""Opt-in, read-only model trial. Does not use scanner result/seen caches."""
import argparse
import contextlib
import copy
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import subprocess
import sys
import threading
import time

import scanner

BASELINE = 'qwen3.5:9b'
CANDIDATE = 'hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ4_XS'
TRIAL_DIR = Path(__file__).resolve().parent / '.model_trials'
SYNTHETIC = dict(id='synthetic', subject='Software Developer Intern — Summer 2027',
                 **{'from': 'careers@example.com'}, date='2026-10-05',
                 body='ExampleCo offers a Software Developer Internship in Toronto, Canada, '
                      'May to August 2027. Build Python backend services. Apply by October 30.')


def memory_snapshot():
    snapshot = {'at': datetime.now(timezone.utc).isoformat()}
    for name, command in [('pressure', ['memory_pressure', '-Q']),
                          ('swap', ['sysctl', 'vm.swapusage']),
                          ('pressure_level', ['sysctl', 'kern.memorystatus_vm_pressure_level'])]:
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=5)
            snapshot[name] = (p.stdout + p.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            snapshot[name] = str(exc)
    return snapshot


def unload(model):
    response = scanner.requests.post(f'{scanner.OLLAMA_URL}/api/generate',
                                     json={'model': model, 'keep_alive': 0}, timeout=180)
    response.raise_for_status()


def evaluate(model, emails):
    previous = scanner.OLLAMA_MODEL
    scanner.OLLAMA_MODEL = model
    samples = [memory_snapshot()]
    stop = threading.Event()

    def monitor():
        while not stop.wait(10):
            samples.append(memory_snapshot())

    worker = threading.Thread(target=monitor, daemon=True)
    worker.start()
    started = time.monotonic()
    log = io.StringIO()
    console = sys.stdout

    class Tee:
        def write(self, text):
            log.write(text)
            return console.write(text)

        def flush(self):
            console.flush()

    try:
        print(f'Analyzing with {model}', flush=True)
        with contextlib.redirect_stdout(Tee()):
            results = scanner.analyze_with_ollama(copy.deepcopy(emails), prefilter=False)
        return {'model': model, 'elapsed_seconds': time.monotonic() - started,
                'results': list(results), 'failed_email_ids': sorted(results.failed_ids),
                'successful_email_ids': sorted(results.successful_ids), 'memory': samples,
                'analysis_log': log.getvalue()}
    finally:
        stop.set()
        worker.join()
        samples.append(memory_snapshot())
        scanner.OLLAMA_MODEL = previous
        unload(model)


def compare(emails, baseline, candidate, checkpoint=None):
    runs = []
    for model in (baseline, candidate):
        runs.append(evaluate(model, emails))
        if checkpoint:
            checkpoint(runs)
    common = set(runs[0]['successful_email_ids']) & set(runs[1]['successful_email_ids'])
    kept = [{r['id'] for r in run['results']} for run in runs]
    disagreements = [{'id': e['id'], 'subject': e['subject'],
                      'baseline_kept': e['id'] in kept[0], 'candidate_kept': e['id'] in kept[1]}
                     for e in emails if e['id'] in common and
                     ((e['id'] in kept[0]) != (e['id'] in kept[1]))]
    return {'runs': runs, 'disagreements': disagreements}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default=BASELINE)
    parser.add_argument('--candidate', default=CANDIDATE)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--max-emails', type=int, default=20)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.days < 1 or not 1 <= args.max_emails <= 500:
        parser.error('--days must be positive; --max-emails must be between 1 and 500')
    output = (args.output or TRIAL_DIR / (datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')).resolve()
    if not output.is_relative_to(TRIAL_DIR.resolve()) or output.suffix != '.json':
        parser.error('--output must be a .json file inside cli/.model_trials/')
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'days': args.days,
              'max_emails': args.max_emails, 'baseline': args.baseline,
              'candidate': args.candidate, 'synthetic': [], 'status': 'preflight'}

    def save():
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')

    previous = scanner.OLLAMA_MODEL
    try:
        for model in (args.baseline, args.candidate):
            scanner.OLLAMA_MODEL = model
            scanner.check_ollama()
        # Ensure neither trial model remains resident before sequential checks.
        for model in dict.fromkeys((args.baseline, args.candidate)):
            unload(model)
        for model in (args.baseline, args.candidate):
            run = evaluate(model, [SYNTHETIC])
            report['synthetic'].append(run)
            save()
            if run['failed_email_ids']:
                raise RuntimeError(f'Synthetic scanner evaluation failed for {model}; Gmail not accessed')
        service = scanner.get_gmail_service(write_access=False)
        emails = scanner.fetch_aggregator_emails(service, days=args.days, unread_only=True,
                                                max_results=args.max_emails)
        report['sample'] = emails
        report['status'] = 'analyzing'
        save()
        if not emails:
            raise RuntimeError('No sample returned; Gmail search may have failed or no emails matched')
        def checkpoint(runs):
            report['runs'] = runs
            save()

        report.update(compare(emails, args.baseline, args.candidate, checkpoint))
        report['status'] = 'failed' if any(r['failed_email_ids'] for r in report['runs']) else 'completed'
        save()
        print(json.dumps({k: v for k, v in report.items() if k not in ('sample', 'synthetic')}, indent=2))
        return 1 if report['status'] == 'failed' else 0
    except (Exception, SystemExit) as exc:
        report.update(status='blocked', error=str(exc))
        save()
        print(f'Trial blocked: {exc}')
        return 1
    finally:
        scanner.OLLAMA_MODEL = previous
        print(f'Trial artifact: {output}')


if __name__ == '__main__':
    raise SystemExit(main())
