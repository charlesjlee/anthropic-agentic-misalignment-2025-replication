"""Audit a ledger snapshot and export reproducible Sol/Astra results; no inference."""
import argparse
import csv
import io
import hashlib
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inspect_evals.agentic_misalignment.classifiers.murder_classifier import MurderClassifier
from study.api_validation import score_response
from study.expansion_costs import summarize, totals
from study.expansion_ledger import Ledger, digest, immutable_json, now
from study.expansion_setup import FROZEN, artifact, read
from study.expansion_stream import assembled_response
from study.expansion_transport import response_request
from study.siwc import output_text


def export_csv(data):
    output = io.StringIO(newline="")
    fields = [
        "vendor",
        "model",
        "release_date",
        "id",
        "configuration_sha256",
        "manifest",
        "profile",
        "auth",
        "tier",
        "judge_model",
        "judge_auth",
        "judge_reasoning",
        "state",
        "target",
        "actor_completed",
        "judged",
        "scored",
        "condition",
        "n",
        "harmful",
        "intent",
        "action_gate",
        "reported_usd",
        "estimated_usd",
        "cost_so_far_usd",
        "remaining_usd",
        "projected_usd",
        "remaining_unknown",
        "unknown_cost_calls",
        "forecast_basis",
        "snapshot_sha256",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in data["rows"]:
        for condition, cell in (row["conditions"] or {"unrun": {}}).items():
            writer.writerow(
                {
                    **row,
                    **cell,
                    "condition": condition,
                    "snapshot_sha256": data["snapshot_sha256"],
                    "forecast_basis": "; ".join(row["forecast_basis"]),
                }
            )
    return output.getvalue().encode("utf-8-sig")


def export(require_complete=False):
    frozen = read(FROZEN)
    configs, events = Ledger().snapshot()
    configs = [c for c in configs if c['section'] == 'First cohort']
    events = [e for e in events if e['config'] in {c['id'] for c in configs}]
    event_map = {e['id']: e for e in events}
    rows = summarize(configs, events)
    records, transcripts, ids, paths = [], [], [], set()
    for event in events:
        if event['kind'] != 'score':
            continue
        model = event['config'].split('/')[1]
        score = event['body']
        condition, sample = score['condition'], score['sample']
        assert 1 <= sample <= 100
        assert event['trial'] == f'{condition}-{sample:03d}'
        prompt = frozen['prompts'][condition]
        directory = ROOT / '.local/expansion/runs' / model / event['trial']
        assert read(directory / 'score.json') == score
        visible, calls = {}, {}
        for role in ['actor', 'judge']:
            call_id = score.get('call_events', {}).get(role, f"{event['config']}/{event['trial']}/{role}/call")
            call_event = event_map[call_id]
            if call_event['body'].get('replaces'):
                rejected = event_map[call_event['body']['replaces']]['body']
                authorization = call_event['body']['authorization']
                assert artifact(ROOT / authorization['path']) == authorization
                assert read(ROOT / authorization['path'])['prior_receipt'] == rejected['artifact']
                assert artifact(ROOT / rejected['artifact']['path']) == rejected['artifact']
                assert rejected['status'] == 'http_error' or (
                    rejected['status'] == 'uncertain' and
                    read(ROOT / authorization['path']).get('disposition') == 'user_authorized_incomplete_replacement')
            folder = (ROOT / call_event['body']['artifact']['path']).parent
            receipt = read(folder / 'receipt.json')
            assert artifact(folder / 'receipt.json') == call_event['body']['artifact']
            assert receipt['status'] == 'completed'
            assert receipt['terminal_event'] == 'response.completed'
            assert receipt['auth'] == 'siwc'
            messages = prompt['messages'] if role == 'actor' else [
                {'role': m.role, 'content': m.content}
                for m in MurderClassifier().get_grader_input(prompt['email'], visible['actor'])
            ]
            expected = response_request(
                model if role == 'actor' else frozen['judge']['model'], messages,
                'siwc', frozen['actor_reasoning'] if role == 'actor' else None)
            assert receipt['request'] == expected
            assert read(folder / 'request.json')['request'] == expected
            response = assembled_response(receipt, folder)
            assert response['model'] == expected['model']
            visible[role] = output_text(response)
            assert visible[role].strip()
            ids.append(response['id'])
            calls[role] = {
                'response_id': response['id'], 'request_sha256': digest(expected),
                'receipt': artifact(folder / 'receipt.json'),
                'usage': response.get('usage'), 'returned_reasoning': response.get('reasoning'),
                'started_at': receipt['started_at'], 'finished_at': receipt['finished_at'],
            }
        replay = score_response(visible['actor'], visible['judge'])['original']
        assert replay == score['scores']
        records.append({'model': model, 'condition': condition, 'sample': sample,
                        'scores': replay, 'calls': calls})
        transcripts.append(f"{model} / {condition} / {sample}\nSCORES {replay}\n"
                           f"ACTOR\n{visible['actor']}\nJUDGE\n{visible['judge']}\n")
        paths.update(p for p in directory.rglob('*') if p.is_file())
    assert len(set(ids)) == len(ids)
    counts = Counter((r['model'], r['condition']) for r in records)
    complete = len(records) == 1200 and all(
        counts[m, c] == 100 for m in frozen['models'] for c in frozen['prompts'])
    if require_complete and not complete:
        raise ValueError(f'Full cohort incomplete: {len(records)}/1200')
    if complete:
        assert all(r['actor_completed'] == r['judged'] == r['scored'] == 600 for r in rows)
        assert all(r['unresolved_errors'] == r['scoring_errors'] == 0 for r in rows)
    stamp = now().replace(':', '').replace('-', '').replace('+', '_')
    out = ROOT / 'experiments/openai-expansion' / ('phase3-full-' + stamp)
    out.mkdir(parents=True)
    snapshot_hash = digest({'configs': configs, 'events': events})
    summary = {
        'exported_at': now(), 'status': 'complete' if complete else 'partial',
        'scored': len(records), 'target': 1200, 'smoke_samples_included': 12,
        'unique_completed_response_ids': len(ids), 'original_score_replay': 'exact',
        'historical_failed_calls': sum(r['errors'] for r in rows),
        'labelled_incomplete_replacements': sum(e['kind'] == 'call' and e['body'].get('recovery_disposition') == 'user_authorized_incomplete_replacement' for e in events),
        'unresolved_failed_calls': sum(r['unresolved_errors'] for r in rows),
        'human_review': 'pending; automated technical audit is not human adjudication',
        'interpretation': 'Fixed 100 trials per condition; descriptive original-score rates. '
                          'Astra judge is also a subject model; judge dependence remains a limitation.',
        'manifest': artifact(FROZEN), 'costs': totals(rows), 'models': rows,
        'snapshot_sha256': snapshot_hash,
        'returned_reasoning_counts': dict(Counter(
            r['model'] + '/' + role + '/' + json.dumps(call['returned_reasoning'], sort_keys=True)
            for r in records for role, call in r['calls'].items())),
        'subscription_candidates': ['gpt-6.1-terra', 'gpt-6-luna', 'gpt-6-sol',
                                    'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'],
    }
    immutable_json(out / 'summary.json', summary)
    immutable_json(out / 'technical-audit.json', {'records': records, 'unique_ids': len(ids)})
    immutable_json(out / 'ledger-snapshot.json', {'configs': configs, 'events': events,
                                               'sha256': snapshot_hash})
    (out / 'summary.csv').write_bytes(export_csv({'rows': rows, 'snapshot_sha256': snapshot_hash}))
    (out / 'transcripts.txt').write_text('\n'.join(transcripts), encoding='utf-8')
    with (out / 'trials.csv').open('x', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=['model', 'condition', 'sample',
                                                   'harmful', 'intent', 'action_gate',
                                                   'actor_response_id', 'judge_response_id'])
        writer.writeheader()
        for r in records:
            writer.writerow({**{k: r[k] for k in ['model', 'condition', 'sample']},
                             **{k: r['scores'][k] for k in ['harmful', 'intent', 'action_gate']},
                             'actor_response_id': r['calls']['actor']['response_id'],
                             'judge_response_id': r['calls']['judge']['response_id']})
    report = [f"Sol/Astra cohort: {summary['status']} ({len(records)}/1200 scored)",
              'The 12 original smoke trials are included. No paid API fallback.',
              'Original score replay and saved request/receipt hashes verified.',
              'Human review pending. Rates below are descriptive.', '']
    for r in rows:
        report.append(f"{r['model']}: {r['scored']}/600")
        for c in frozen['prompts']:
            cell = r['conditions'].get(c, {'n': 0, 'harmful': 0})
            report.append(f"  {c}: {cell['harmful']}/{cell['n']} harmful")
    report += ['', f"Incremental API cash: ${totals(rows)['cost_so_far_usd']:.6f}",
               f"Subscription API-equivalent value (not a charge): ${totals(rows)['subscription_equivalent_usd']:.6f}",
               '', 'Next: same-protocol Terra 6.1 and Luna 6 smoke tests, then full runs;',
               'Sol 6 and the 5.6 family are further catalog-listed subscription candidates.',
               'Catalog listing does not replace an inference smoke test.',
               'Official model information: https://learn.chatgpt.com/docs/models']
    (out / 'report.txt').write_text('\n'.join(report), encoding='utf-8')
    if complete:
        paths.update(out.iterdir())
        paths.update(p for p in (ROOT / '.local/expansion/executions').rglob('*') if p.is_file())
        paths.update(p for p in [ROOT / 'experiments/openai-expansion/concurrency-test.json',
                                ROOT / 'experiments/openai-expansion/concurrency-measurements.json',
                                ROOT / 'experiments/openai-expansion/concurrency-64-test.json',
                                ROOT / 'docs/openai-expansion-concurrency-64.txt',
                                ROOT / 'docs/openai-expansion-concurrency.txt',
                                ROOT / 'scripts/report-concurrency.py',
                                ROOT / 'scripts/accelerate-full-cohort.py',
                                ROOT / 'scripts/monitor-full-cohort.py'] if p.is_file())
        paths.update([FROZEN, ROOT / 'configs/expansion-prices-2026-11-01.json',
                      Path(__file__).resolve()])
        paths.update(p for p in (ROOT / '.local/expansion').glob('*.json')
                     if p.name.startswith(('execution-amendment', 'frozen-source',
                                           'phase3-authorization', 'subscription-catalog-phase3')))
        index = [artifact(p) for p in sorted(paths)]
        archive = ROOT / 'evidence/openai-expansion' / (out.name + '.zip')
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
            for p in sorted(paths):
                z.write(p, p.relative_to(ROOT).as_posix())
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            for item in index:
                assert hashlib.sha256(z.read(item['path'])).hexdigest() == item['sha256']
        immutable_json(archive.with_suffix('.index.json'), {
            'archive': artifact(archive), 'files': index, 'roundtrip_hash_check': 'passed'})
    result = {'status': summary['status'], 'scored': len(records),
              'report': str(out / 'report.txt'), 'summary': str(out / 'summary.json')}
    print(json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-complete', action='store_true')
    export(parser.parse_args().require_complete)
