"""Bounded scheduling: no duplicate submission and no refill after a failure."""

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def bounded_trials(items, work, workers, stopped=lambda: False):
    if not 1 <= workers <= 64:
        raise ValueError("Concurrency must be between 1 and 64")
    failed = threading.Event()
    iterator = iter(items)
    started = time.monotonic()
    submitted = completed = 0
    errors = []
    exhausted = False

    def perform(item):
        try:
            return work(item)
        except Exception:
            failed.set()
            raise

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        while pending or not exhausted:
            while len(pending) < workers and not exhausted:
                if failed.is_set() or stopped():
                    exhausted = True
                    break
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending[pool.submit(perform, item)] = item
                submitted += 1
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    future.result()
                    completed += 1
                except Exception as exc:  # noqa: BLE001 -- drain active work and preserve failure diagnostics
                    errors.append(
                        {"job": item, "type": type(exc).__name__, "message": str(exc)}
                    )
    return {
        "submitted": submitted,
        "completed": completed,
        "errors": errors,
        "workers": workers,
        "elapsed_seconds": time.monotonic() - started,
        "stopped": stopped(),
    }
