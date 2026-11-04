"""Ramp bounded concurrency using remaining cohort samples, then finish at measured capacity."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from study.expansion_ledger import immutable_json, now
from study.expansion_run import run


def main():
    state = {'started_at': now(), 'status': 'testing', 'stages': [],
             'baseline': {'workers': 1, 'actor_mean_seconds': 32.68,
                          'judge_mean_seconds': 7.4, 'scored_per_minute': 60 / 40.08},
             'method': 'Remaining scheduled cohort trials; fixed requests/scoring; no retry or paid fallback'}
    state_file = ROOT / '.local/expansion/concurrency-test.json'

    def publish():
        temp = state_file.with_suffix('.tmp')
        temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
        os.replace(temp, state_file)

    # The two-worker stage is run and checked interactively before this process starts.
    publish()
    for workers in [4, 8, 16, 32]:
        state['active_workers'] = workers
        publish()
        result = run(count=100, execute=True, workers=workers, limit=workers * 2)
        rate = result['completed'] * 60 / max(result['elapsed_seconds'], 0.001)
        state['stages'].append({**result, 'scored_per_minute': rate})
        publish()
        print(json.dumps({'workers': workers, 'completed': result['completed'],
                          'elapsed_seconds': result['elapsed_seconds'], 'scored_per_minute': rate,
                          'errors': result['errors']}), flush=True)
        if result['errors'] or result['stopped'] or result['completed'] != workers * 2:
            state['status'] = 'paused_for_reconciliation'
            publish()
            return
    # Use the smaller level if the largest tested level offers under 10% extra throughput.
    selected = max(state['stages'], key=lambda s: s['scored_per_minute'])
    for stage in state['stages']:
        if stage['scored_per_minute'] >= selected['scored_per_minute'] / 1.10:
            selected = stage
            break
    state['selected_workers'] = selected['workers']
    state['selected_scored_per_minute'] = selected['scored_per_minute']
    state['status'] = 'full_run'
    state['interpretation'] = 'Measured successful concurrency, not proof of an exact provider ceiling or remaining quota'
    publish()
    immutable_json(ROOT / 'experiments/openai-expansion/concurrency-test.json', state)
    print(json.dumps({'selected_workers': selected['workers'], 'status': 'full_run'}), flush=True)
    result = run(count=100, execute=True, workers=selected['workers'])
    state['full_run_result'] = result
    state['status'] = result['status']
    state['finished_at'] = now()
    publish()
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
