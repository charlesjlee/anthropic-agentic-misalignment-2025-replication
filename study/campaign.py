"""Checkpointed historical campaign; fixed sample IDs, no automatic paid retries."""

import argparse
import hashlib
import json
import random
import re
import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

import httpx
from filelock import FileLock
from inspect_ai.model import GenerateConfig
from inspect_evals.agentic_misalignment.classifiers.murder_classifier import (
    MurderClassifier,
)

from study import siwc
from study.api_validation import JUDGE, request_for, score_response, validate_response
from study.auth import ROOT
from study.credentials import read_key
from study.export_evidence import screen
from study.historical_audit import CONDITIONS
from study.openrouter_smoke import BASE, PROVIDERS, checked_completion
from study.pilot import read, save, verify_sources
from study.smoke import provenance

APPROVAL = "okay I'm ready, execute the hsitoirca repro as you described. no need to set stop limits becuase I already have them on the API side"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _subject_trial_paths(directory):
    """Select a single reviewed retry without modifying the rejected attempt."""
    subject, result = directory / "subject.json", directory / "result.json"
    approval = directory / "reconciliation.json"
    if not approval.exists():
        return subject, result
    record = read(approval)
    raw = read(subject)
    if (
        record.get("decision") != "retry_rejected_429_once"
        or raw.get("status") != "failed_or_uncertain"
        or raw.get("http_status") != 429
        or raw.get("response") is not None
        or raw.get("reported_cost_usd", 0)
        or raw.get("estimated_cost_usd_no_cache_discount", 0)
        or record.get("subject_sha256")
        != hashlib.sha256(subject.read_bytes()).hexdigest()
        or record.get("result_sha256")
        != hashlib.sha256(result.read_bytes()).hexdigest()
        or read(result).get("status") != "failed_or_unscored"
        or (directory / "judge-attempt.json").exists()
        and not (directory / "subject-retry-002.json").exists()
    ):
        raise ValueError("Invalid reviewed rejection reconciliation")
    return directory / "subject-retry-002.json", directory / "result-retry-002.json"


def trial_paths(directory):
    subject, result = _subject_trial_paths(directory)
    approval = directory / "scoring-reconciliation.json"
    if approval.exists():
        record = read(approval)
        if (
            record.get("decision")
            not in {
                "regrade_saved_subject_after_empty_overload",
                "regrade_saved_subject_after_interrupted_judge",
            }
            or read(subject).get("status") != "completed"
            or read(result).get("status") != "failed_or_unscored"
            or record.get("subject_sha256")
            != hashlib.sha256(subject.read_bytes()).hexdigest()
            or record.get("result_sha256")
            != hashlib.sha256(result.read_bytes()).hexdigest()
        ):
            raise ValueError("Invalid scoring reconciliation")
        result = directory / "result-rescore-002.json"
    return subject, result


def execution_settings(directory, manifest):
    settings = {
        "workers": manifest["workers"],
        "concurrency": {
            "openai": 4,
            "siliconflow/fp8": 2,
            "parasail/fp8": 2,
            "judge": 4,
        },
    }
    path = directory / "execution-amendment.json"
    if path.exists():
        amendment = read(path)
        if (
            amendment.get("manifest_sha256")
            != hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
        ):
            raise ValueError("Execution amendment manifest mismatch")
        settings = {k: amendment[k] for k in ("workers", "concurrency")}
    if (
        set(settings["concurrency"])
        != {"openai", "siliconflow/fp8", "parasail/fp8", "judge"}
        or any(
            type(v) is not int or not 1 <= v <= 32
            for v in settings["concurrency"].values()
        )
        or type(settings["workers"]) is not int
        or not 1 <= settings["workers"] <= 32
    ):
        raise ValueError("Invalid execution concurrency")
    return settings


def pop_ready(pending, active, limits, lane_for):
    """Keep provider queues independent while preserving order within a lane."""
    for index, job in enumerate(pending):
        if active[lane_for(job)] < limits[lane_for(job)]:
            return pending.pop(index)
    return None


def rejected_before_generation(receipt):
    return (
        receipt.get("route") == BASE + "/chat/completions"
        and receipt.get("http_status") == 429
        and receipt.get("api_error_code") == 429
        and receipt.get("response") is None
        and not receipt.get("api_error_has_generation")
        and not receipt.get("reported_cost_usd")
        and not receipt.get("estimated_cost_usd_no_cache_discount")
    )


def empty_judge_overload(receipt):
    events = receipt.get("events", [])
    failed = [
        e.get("response", {}) for e in events if e.get("type") == "response.failed"
    ]
    return (
        receipt.get("status") == "response.failed"
        and receipt.get("terminal_event") == "response.failed"
        and len(failed) == 1
        and failed[0].get("status") == "failed"
        and failed[0].get("model") == JUDGE
        and failed[0].get("output") == []
        and failed[0].get("usage") is None
        and failed[0].get("error", {}).get("code") == "server_is_overloaded"
        and not any(
            e.get("type", "").startswith(("response.output", "response.reasoning"))
            for e in events
        )
    )


def reviewed_interrupted_judge(call, directory):
    approval = directory / "scoring-reconciliation.json"
    if not approval.exists():
        return False
    record, raw = read(approval), read(call)
    return (
        record.get("decision") == "regrade_saved_subject_after_interrupted_judge"
        and record.get("failed_judge_receipt") == call.name
        and record.get("failed_judge_sha256")
        == hashlib.sha256(call.read_bytes()).hexdigest()
        and record.get("retrieval_http_status") in {401, 404}
        and raw.get("auth_route") == "siwc"
        and raw.get("http_status") == 200
        and raw.get("status") == "failed_before_completion"
        and raw.get("error_type") == "RuntimeError"
        and raw.get("request", {}).get("model") == JUDGE
        and raw.get("request", {}).get("store") is False
        and not raw.get("terminal_event")
        and bool(raw.get("events"))
        and not any(
            e.get("type")
            in {"response.completed", "response.failed", "response.incomplete", "error"}
            for e in raw["events"]
        )
    )


def make_jobs(plan, kind):
    jobs = []
    for index, model in enumerate(plan["models"]):
        if kind == "preflight" and model["route"] != "openrouter":
            continue
        for condition in CONDITIONS:
            if kind == "preflight" and condition == CONDITIONS[0]:
                continue
            for sample in range(1, (100 if kind == "historical" else 1) + 1):
                jobs.append(
                    {
                        "id": f"m{index + 1}-{condition['name']}-{sample:03d}",
                        "model": model["model"],
                        "route": model["route"],
                        "pair": f"m{index + 1}-{condition['name']}",
                        "condition": condition,
                        "sample": sample,
                    }
                )
    random.Random(plan["sample_policy"]["scheduler_seed"]).shuffle(jobs)
    return jobs


def build_pairs(plan):
    routes = {
        m["model"]: m
        for m in read(ROOT / "configs/openrouter-validation.json")["models"]
    }
    pairs = {}
    for index, model in enumerate(plan["models"]):
        for condition in CONDITIONS:
            request, email = request_for(
                model["model"] if model["route"] == "openai" else "gpt-4.1-2025-04-14",
                condition,
            )
            if model["route"] == "openrouter":
                request = {
                    "model": model["model"],
                    "messages": request["messages"],
                    "temperature": 1.0,
                    "max_tokens": 10000,
                    "transforms": [],
                    "provider": {
                        k: v
                        for k, v in routes[model["model"]]["provider"].items()
                        if k != "max_price"
                    },
                }
            pairs[f"m{index + 1}-{condition['name']}"] = {
                "request": request,
                "email": email,
                "sha256": digest(request),
            }
    return pairs


def freeze_source(directory, commit):
    names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", commit], cwd=ROOT, text=True
    ).splitlines()
    for name in names:
        if name.startswith(("study/", "configs/")) or name in {
            "uv.lock",
            "pyproject.toml",
            "docs/historical-prompt-audit.json",
            "docs/campaign-protocol.md",
        }:
            path = directory / "frozen-source" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                subprocess.check_output(["git", "show", commit + ":" + name], cwd=ROOT)
            )


def initialize(kind, directory, preflight=None):
    plan = read(ROOT / "configs/historical-run-plan.json")
    if plan["status"] != "authorized_2026-10-29_provider_side_spending_limits":
        raise ValueError("Campaign not authorized")
    audit = read(ROOT / "docs/historical-prompt-audit.json")
    if not audit["components_equal"] or not audit["judge_prompt_constants_equal"]:
        raise ValueError("Prompt audit not passed")
    if kind == "historical":
        if (
            preflight is None
            or read(preflight / "status.json")["status"] != "completed"
        ):
            raise ValueError("Remaining OpenRouter validation has not passed")
        status = read(preflight / "status.json")
        if status["planned"] != 10 or status["scored"] != 10:
            raise ValueError("Preflight completeness mismatch")
    registry = ROOT / ".local/campaign-registry.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(registry) + ".lock", timeout=5):
        registered = read(registry) if registry.exists() else {}
        if kind in registered:
            raise FileExistsError(
                "This phase is already registered; resume its saved directory"
            )
        directory.mkdir(parents=True, exist_ok=False)
        registered[kind] = {
            "directory": str(directory.resolve()),
            "registered_at": time.time(),
        }
        save(registry, registered)
    jobs, pairs = make_jobs(plan, kind), build_pairs(plan)
    prov = provenance()
    save(directory / "plan.json", plan)
    save(directory / "jobs.json", jobs)
    save(directory / "prompts.json", pairs)
    manifest = {
        "kind": kind,
        "created_at": datetime.now(UTC).isoformat(),
        "approval": APPROVAL,
        "provenance": prov,
        "source_hashes": verify_sources(),
        "jobs_sha256": digest(jobs),
        "prompts_sha256": digest(pairs),
        "plan_sha256": digest(plan),
        "planned": len(jobs),
        "preflight": str(preflight) if preflight else None,
        "workers": 8 if kind == "historical" else 4,
        "concurrency": {"openai": 4, "siliconflow": 2, "parasail": 2, "siwc_judge": 4},
        "local_dollar_limit": None,
        "spending_limits": "Provider-side, as requested by user",
        "retries": "No automatic subject or judge retry; uncertain attempts require reconciliation",
        "judge": JUDGE,
        "judge_route": "siwc",
        "judge_settings": plan["judge"],
    }
    save(directory / "manifest.json", manifest)
    freeze_source(directory, prov["study_commit"])
    save(
        directory / "status.json",
        {"status": "prepared", "planned": len(jobs), "scored": 0},
    )
    return manifest


def load_inputs(directory):
    manifest, jobs, pairs, plan = (
        read(directory / f)
        for f in ("manifest.json", "jobs.json", "prompts.json", "plan.json")
    )
    for data, field in [
        (jobs, "jobs_sha256"),
        (pairs, "prompts_sha256"),
        (plan, "plan_sha256"),
    ]:
        if digest(data) != manifest[field]:
            raise ValueError("Frozen campaign input changed")
    return manifest, jobs, pairs, plan


def precheck(directory, plan, keys):
    for model in plan["models"]:
        if model["route"] != "openrouter":
            continue
        result = httpx.get(
            BASE + "/models/" + model["model"] + "/endpoints", timeout=30
        )
        result.raise_for_status()
        catalog = result.json()
        matches = [
            r for r in catalog["data"]["endpoints"] if r["tag"] == model["provider"]
        ]
        if (
            len(matches) != 1
            or matches[0]["quantization"] != "fp8"
            or not {"temperature", "max_tokens"}.issubset(
                matches[0]["supported_parameters"]
            )
        ):
            raise ValueError("Fixed endpoint controls/precision unavailable")
        path = directory / "catalogs" / (model["model"].replace("/", "__") + ".json")
        if not path.exists():
            save(path, catalog)
    catalog = siwc.catalog()
    if JUDGE not in [
        m["slug"] for m in catalog["models"] if m.get("visibility") == "list"
    ]:
        raise ValueError("Astra unavailable; no fallback")
    # Credentials are decrypted before any subject request, never saved in evidence.
    if any(not value for value in keys.values()):
        raise ValueError("Missing project key")


class Runner:
    def __init__(
        self, directory, pairs, plan, keys, concurrency=None, retry_policy=None
    ):
        self.directory, self.pairs, self.keys = directory, pairs, keys
        self.models = {m["model"]: m for m in plan["models"]}
        self.stop = threading.Event()
        self.concurrency = concurrency or {
            "openai": 4,
            "siliconflow/fp8": 2,
            "parasail/fp8": 2,
            "judge": 4,
        }
        self.limits = {k: threading.Semaphore(v) for k, v in self.concurrency.items()}
        self.retry_policy = retry_policy or {}
        self.cooldown_lock = threading.Lock()
        self.next_allowed = Counter()

    def wait_for_lane(self, lane, respect_stop=True):
        while not respect_stop or not self.stop.is_set():
            with self.cooldown_lock:
                delay = self.next_allowed[lane] - time.time()
                if delay <= 0:
                    spacing = self.retry_policy.get("minimum_interval_seconds", {}).get(
                        lane, 0
                    )
                    self.next_allowed[lane] = time.time() + spacing
                    return True
            if respect_stop:
                self.stop.wait(min(delay, 30))
            else:
                time.sleep(min(delay, 30))
        return False

    def lane(self, job):
        return (
            "openai"
            if job["route"] == "openai"
            else self.models[job["model"]]["provider"]
        )

    def persist(self, path, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        screen(path, data, list(self.keys.values()))
        save(path, value)

    def subject(self, job, path):
        if not self.retry_policy.get("retry_rejected_openrouter_429") or path.exists():
            return self.subject_once(job, path)
        attempts = path.parent / (path.stem + "-attempts")
        previous = sorted(
            p for p in attempts.glob("*.json") if not p.name.endswith(".backoff.json")
        )
        # A crash between receiving a response and publishing the canonical receipt
        # must reuse that response; an uncertain attempt must never be purchased again.
        for prior in previous:
            raw = read(prior)
            if raw.get("request") != self.pairs[job["pair"]]["request"]:
                raise ValueError("Attempt request mismatch")
            if raw.get("status") == "completed":
                self.persist(path, raw)
                return self.subject_once(job, path)
            if not rejected_before_generation(raw):
                self.stop.set()
                raise RuntimeError("Uncertain saved attempt requires reconciliation")
        rejected = len(previous)
        for backoff in attempts.glob("*.backoff.json"):
            with self.cooldown_lock:
                lane = self.lane(job)
                self.next_allowed[lane] = max(
                    self.next_allowed[lane], read(backoff)["resume_after"]
                )
        while not self.stop.is_set():
            attempt = attempts / (
                f"{len(previous) + 1:05d}-" + uuid.uuid4().hex + ".json"
            )
            try:
                subject = self.subject_once(job, attempt)
                if subject is not None:
                    self.persist(path, read(attempt))
                return subject
            except Exception:
                raw = read(attempt) if attempt.exists() else {}
                if not rejected_before_generation(raw):
                    if raw:
                        self.persist(path, raw)
                    self.stop.set()
                    raise
                rejected += 1
                previous.append(attempt)
                wait_seconds = max(
                    raw.get("retry_after_seconds", 0),
                    min(900, 60 * 2 ** min(rejected - 1, 4)),
                )
                lane = self.lane(job)
                until = time.time() + wait_seconds
                with self.cooldown_lock:
                    self.next_allowed[lane] = max(self.next_allowed[lane], until)
                self.persist(
                    attempt.with_suffix(".backoff.json"),
                    {
                        "sample_id": job["id"],
                        "rejected_attempt": attempt.name,
                        "resume_after": until,
                        "wait_seconds": wait_seconds,
                        "reason": "Explicit OpenRouter HTTP 429 before generation; no response/usage. Other providers continue.",
                    },
                )
                print(
                    f"Rate limit {job['id']}: provider cooldown {wait_seconds}s; preserving rejected attempt",
                    flush=True,
                )
        return None

    def subject_once(self, job, path):
        request = self.pairs[job["pair"]]["request"]
        model = self.models[job["model"]]
        if path.exists():
            receipt = read(path)
            if receipt["request"] != request or receipt.get("status") != "completed":
                raise RuntimeError(
                    "Existing uncertain/failed subject request; no replay"
                )
            raw = receipt["response"]
        else:
            lane = "openai" if job["route"] == "openai" else model["provider"]
            with self.limits[lane]:
                if not self.wait_for_lane(lane):
                    return None
                route = (
                    "https://api.openai.com/v1/chat/completions"
                    if job["route"] == "openai"
                    else BASE + "/chat/completions"
                )
                receipt = {
                    "status": "started",
                    "sample_id": job["id"],
                    "request": request,
                    "route": route,
                    "started_at": time.time(),
                    "request_sha256": digest(request),
                }
                self.persist(path, receipt)
                try:
                    response = httpx.post(
                        route,
                        json=request,
                        headers={"Authorization": "Bearer " + self.keys[job["route"]]},
                        timeout=httpx.Timeout(600, connect=30),
                    )
                    receipt["http_status"] = response.status_code
                    receipt["request_id"] = response.headers.get("x-request-id")
                    # Numeric retry metadata only; never persist arbitrary headers.
                    retry_after = response.headers.get("retry-after", "")
                    if re.fullmatch(r"[0-9]{1,8}", retry_after):
                        receipt["retry_after_seconds"] = int(retry_after)
                    if response.status_code != 200:
                        try:
                            body = response.json()
                            receipt["api_error_has_generation"] = any(
                                body.get(k)
                                for k in ("choices", "usage", "id", "output")
                            )
                            error = body.get("error", {})
                            code = error.get("code")
                            if (
                                isinstance(code, int)
                                or isinstance(code, str)
                                and re.fullmatch(r"[a-z_]{1,80}", code)
                            ):
                                receipt["api_error_code"] = code
                            # Preserve useful provider diagnostics, with secrets redacted.
                            message = str(error.get("message", ""))[:2000]
                            for secret in self.keys.values():
                                message = message.replace(secret, "[redacted]")
                            message = re.sub(
                                r"sk-[A-Za-z0-9_-]+", "[redacted]", message
                            )
                            receipt["api_error_message"] = message
                        except (ValueError, AttributeError):
                            pass
                        raise RuntimeError("Subject API error")
                    raw = response.json()
                    receipt["response"] = raw
                    usage = raw.get("usage", {})
                    if all(
                        isinstance(usage.get(k), int)
                        for k in ("prompt_tokens", "completion_tokens")
                    ):
                        pi, po = model["price_per_million_input_output"]
                        receipt["estimated_cost_usd_no_cache_discount"] = (
                            usage["prompt_tokens"] * pi
                            + usage["completion_tokens"] * po
                        ) / 1e6
                        receipt["reported_cost_usd"] = usage.get("cost")
                    self.persist(
                        path, receipt
                    )  # Preserve raw response before validation.
                    if job["route"] == "openai":
                        validate_response(raw, job["model"])
                    else:
                        checked_completion(
                            raw, job["model"], PROVIDERS[model["provider"]]
                        )
                    receipt["status"] = "completed"
                except Exception as exc:
                    receipt.update(
                        status="failed_or_uncertain", error_type=type(exc).__name__
                    )
                    if not (
                        self.retry_policy.get("retry_rejected_openrouter_429")
                        and rejected_before_generation(receipt)
                    ):
                        self.stop.set()
                    raise
                finally:
                    receipt["finished_at"] = time.time()
                    self.persist(path, receipt)
        return raw["choices"][0]["message"]["content"]

    def judge(self, job, subject, directory):
        path = directory / "judge.json"
        messages = MurderClassifier().get_grader_input(
            self.pairs[job["pair"]]["email"], subject
        )
        request, _ = siwc.prepare_request(
            JUDGE, messages, GenerateConfig(timeout=600, max_retries=0)
        )
        if path.exists():
            receipt = read(path)
            if receipt["request"] != request or receipt.get("status") != "completed":
                raise RuntimeError("Existing judge output invalid")
            return siwc.output_text(receipt["response"])
        marker = directory / "judge-attempt.json"
        calls = sorted((directory / "judge-calls").glob("*.json"))
        response = None
        rejected = 0
        retry_enabled = self.retry_policy.get(
            "retry_empty_overloaded_siwc_judge", False
        )
        if marker.exists():
            if not calls:
                raise RuntimeError("Uncertain prior judge attempt; no automatic retry")
            for call in calls:
                raw = read(call)
                if raw["request"] != request:
                    raise ValueError("Recovered judge request mismatch")
                if (
                    raw.get("status") == "completed"
                    and raw.get("terminal_event") == "response.completed"
                ):
                    if response is not None:
                        raise ValueError("Multiple completed judge responses")
                    response = raw["response"]
                elif retry_enabled and empty_judge_overload(raw):
                    rejected += 1
                    self.judge_backoff(job, call, rejected)
                elif reviewed_interrupted_judge(call, directory):
                    # A reviewed, hash-bound single attempt, not an automatic
                    # exception for all partial or uncertain judge streams.
                    rejected += 1
                else:
                    raise RuntimeError("Incomplete prior judge attempt")
        elif calls:
            raise RuntimeError("Judge calls exist without attempt marker")
        while response is None:
            if rejected >= 5:
                raise RuntimeError("Repeated judge overload requires investigation")
            with self.limits["judge"]:
                if retry_enabled and not self.wait_for_lane(
                    "judge", respect_stop=False
                ):
                    raise RuntimeError("Judge pending after campaign stop")
                if not marker.exists():
                    self.persist(
                        marker,
                        {
                            "status": "attempt_started",
                            "started_at": time.time(),
                            "request": request,
                        },
                    )
                before = set((directory / "judge-calls").glob("*.json"))
                try:
                    response = siwc.call(
                        JUDGE,
                        messages,
                        GenerateConfig(timeout=600, max_retries=0),
                        evidence_dir=directory / "judge-calls",
                    )
                except Exception:
                    added = set((directory / "judge-calls").glob("*.json")) - before
                    if not retry_enabled or len(added) != 1:
                        raise
                    call = added.pop()
                    raw = read(call)
                    if raw.get("request") != request or not empty_judge_overload(raw):
                        raise
                    rejected += 1
                    self.judge_backoff(job, call, rejected)
        if response.get("model") != JUDGE or response.get("status") != "completed":
            raise ValueError("Judge substitution/incomplete output")
        self.persist(
            path, {"status": "completed", "request": request, "response": response}
        )
        return siwc.output_text(response)

    def judge_backoff(self, job, call, number):
        path = call.parent.parent / "judge-backoffs" / call.name
        if not path.exists():
            delay = min(900, 60 * 2 ** min(number - 1, 4))
            self.persist(
                path,
                {
                    "sample_id": job["id"],
                    "failed_call": call.name,
                    "wait_seconds": delay,
                    "resume_after": read(call)["finished_at"] + delay,
                    "reason": "Explicit Astra server_is_overloaded with empty output and no usage; reuse the paid subject.",
                },
            )
        with self.cooldown_lock:
            self.next_allowed["judge"] = max(
                self.next_allowed["judge"], read(path)["resume_after"]
            )
        print(
            f"Judge overload {job['id']}: saved subject retained; backing off",
            flush=True,
        )

    def trial(self, job):
        directory = self.directory / "trials" / job["id"]
        subject_path, result = trial_paths(directory)
        if result.exists():
            row = read(result)
            if row["status"] != "scored":
                self.stop.set()
                raise RuntimeError("Existing failed trial requires reconciliation")
            return row
        try:
            subject = self.subject(job, subject_path)
            if subject is None:
                return {"id": job["id"], "status": "not_started"}
            judge = self.judge(job, subject, directory)
            scores = score_response(subject, judge)
            row = {
                "id": job["id"],
                "status": "scored",
                "job": job,
                "scores": scores,
                "subject_text": subject,
                "judge_text": judge,
                "human_review": "pending",
            }
            self.persist(result, row)
            return row
        except Exception as exc:  # noqa: BLE001 - preserve type only at credential boundary
            self.stop.set()
            row = {
                "id": job["id"],
                "status": "failed_or_unscored",
                "job": job,
                "error_type": type(exc).__name__,
            }
            self.persist(result, row)
            return row


def summarize(directory, jobs, state):
    counters = Counter()
    costs = Counter()
    cells = {}
    failures = []
    for job in jobs:
        cell = cells.setdefault(
            job["pair"],
            {
                "model": job["model"],
                "condition": job["condition"],
                "planned": 0,
                "attempted": 0,
                "subject_completed": 0,
                "scored": 0,
                "original_harmful": 0,
            },
        )
        cell["planned"] += 1
        path = directory / "trials" / job["id"]
        subject_path, result_path = trial_paths(path)
        if (path / "reconciliation.json").exists():
            counters["reconciled_rejections"] += 1
        if (path / "scoring-reconciliation.json").exists():
            counters["reconciled_judge_trials"] += 1
        if subject_path.exists():
            raw = read(subject_path)
            counters["attempted"] += 1
            cell["attempted"] += 1
            if raw["status"] == "completed":
                counters["subject_completed"] += 1
                cell["subject_completed"] += 1
            cost = raw.get("reported_cost_usd")
            if cost is None:
                cost = raw.get("estimated_cost_usd_no_cache_discount", 0)
            costs[job["route"]] += cost
        if result_path.exists():
            row = read(result_path)
            if row["status"] != "scored":
                failures.append({"id": job["id"], "error_type": row.get("error_type")})
                continue
            counters["scored"] += 1
            cell["scored"] += 1
            scores = row["scores"]
            cell["original_harmful"] += int(scores["original"]["harmful"])
    report = {
        "status": state,
        "updated_at": datetime.now(UTC).isoformat(),
        "planned": len(jobs),
        "attempted": counters["attempted"],
        "reconciled_rejections": counters["reconciled_rejections"],
        "reconciled_judge_trials": counters["reconciled_judge_trials"],
        "automatic_rate_limit_rejections": sum(
            1 for p in (directory / "trials").glob("*/*-attempts/*.backoff.json")
        ),
        "judge_overload_recoveries": sum(
            1 for _ in (directory / "trials").glob("*/judge-backoffs/*.json")
        ),
        "subject_completed": counters["subject_completed"],
        "scored": counters["scored"],
        "failures": failures,
        "cost_usd_reported_or_estimated": dict(costs),
        "cells": cells,
        "human_review": "pending; machine completion is not human adjudication",
    }
    save(directory / "status.json", report)
    return report


def finalize(directory, jobs, keys):
    review = []
    for pair in sorted({j["pair"] for j in jobs}):
        agreements = []
        for job in [j for j in jobs if j["pair"] == pair]:
            _, path = trial_paths(directory / "trials" / job["id"])
            if not path.exists() or read(path)["status"] != "scored":
                continue
            row = read(path)
            scores = row["scores"]
            reasons = []
            if scores["original"]["action_gate"] and not scores["original"]["intent"]:
                reasons.append("code_gate_without_harmful_intent")
            if reasons:
                review.append(
                    {"sample_id": job["id"], "reasons": reasons, "status": "pending"}
                )
            else:
                agreements.append((job["id"], scores["original"]["harmful"]))
        rng = random.Random(
            20261029 + int(hashlib.sha256(pair.encode()).hexdigest()[:8], 16)
        )
        positives = [i for i, label in agreements if label]
        negatives = [i for i, label in agreements if not label]
        rng.shuffle(positives)
        rng.shuffle(negatives)
        selected = positives[:3] + negatives[:2]
        rest = [i for i, _ in agreements if i not in selected]
        rng.shuffle(rest)
        selected += rest[: max(0, 5 - len(selected))]
        review.extend(
            {
                "sample_id": i,
                "reasons": ["prespecified_label_sample"],
                "status": "pending",
            }
            for i in selected
        )
    save(directory / "review-queue.json", review)
    rows = []
    for job in sorted(jobs, key=lambda j: j["id"]):
        _, path = trial_paths(directory / "trials" / job["id"])
        if not path.exists():
            continue
        row = read(path)
        if row["status"] == "scored":
            rows.append(
                "## "
                + row["id"]
                + "\n\n### Subject\n"
                + row["subject_text"]
                + "\n\n### Judge\n"
                + row["judge_text"]
            )
    (directory / "transcripts.md").write_text(
        "# Historical study transcripts\n\n" + "\n\n".join(rows), encoding="utf-8"
    )
    index = []
    for path in sorted(directory.rglob("*")):
        if (
            not path.is_file()
            or "analysis" in path.relative_to(directory).parts
            or path.name == "index.json"
            or path.suffix in {".lock", ".tmp"}
        ):
            continue
        data = path.read_bytes()
        screen(path, data, list(keys.values()))
        index.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    save(
        directory / "index.json",
        {
            "files": index,
            "status": read(directory / "status.json")["status"],
            "note": "Integrity manifest, not provider attestation. Human review pending.",
        },
    )


def execute(directory):
    with FileLock(str(directory / "campaign.lock"), timeout=1):
        manifest, jobs, pairs, plan = load_inputs(directory)
        keys = {route: read_key(route) for route in {j["route"] for j in jobs}}
        precheck(directory, plan, keys)
        # Resolve previous failures before scheduling even one new paid request.
        for job in jobs:
            trial = directory / "trials" / job["id"]
            subject_path, result_path = trial_paths(trial)
            if result_path.exists() and read(result_path)["status"] != "scored":
                raise RuntimeError("Prior failed result requires reconciliation")
            if subject_path.exists() and read(subject_path)["status"] != "completed":
                raise RuntimeError(
                    "Prior uncertain paid request; refusing automatic replay"
                )
        settings = execution_settings(directory, manifest)
        policy_path = directory / "rate-limit-policy.json"
        retry_policy = read(policy_path) if policy_path.exists() else {}
        runner = Runner(
            directory, pairs, plan, keys, settings["concurrency"], retry_policy
        )
        pending = [
            j
            for j in jobs
            if not trial_paths(directory / "trials" / j["id"])[1].exists()
        ]
        report = summarize(directory, jobs, "running")
        print(
            f"Campaign {directory.name}: {report['scored']}/{len(jobs)} already scored",
            flush=True,
        )
        try:
            with ThreadPoolExecutor(max_workers=settings["workers"]) as pool:
                futures = {}
                active = Counter()

                def submit_one():
                    if runner.stop.is_set():
                        return
                    job = pop_ready(pending, active, runner.concurrency, runner.lane)
                    if job is not None:
                        active[runner.lane(job)] += 1
                        futures[pool.submit(runner.trial, job)] = job
                    return job is not None

                for _ in range(settings["workers"]):
                    submit_one()
                while futures:
                    done, _ = wait(futures, timeout=30, return_when=FIRST_COMPLETED)
                    for future in done:
                        job = futures.pop(future)
                        active[runner.lane(job)] -= 1
                        row = future.result()
                        print(job["id"] + ": " + row["status"], flush=True)
                    report = summarize(
                        directory,
                        jobs,
                        "draining_after_error" if runner.stop.is_set() else "running",
                    )
                    print(
                        f"Progress {report['scored']}/{len(jobs)}; subject calls {report['attempted']}; cost {report['cost_usd_reported_or_estimated']}",
                        flush=True,
                    )
                    while len(futures) < settings["workers"] and submit_one():
                        pass
            report = summarize(
                directory,
                jobs,
                "stopped_on_error" if runner.stop.is_set() else "completed",
            )
            if report["scored"] != len(jobs) and report["status"] == "completed":
                report = summarize(directory, jobs, "incomplete")
        finally:
            finalize(directory, jobs, keys)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["preflight", "historical", "resume"])
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--preflight", type=Path)
    args = parser.parse_args()
    directory = args.directory
    if args.kind != "resume":
        if directory is None:
            prefix = (
                "openrouter-remaining" if args.kind == "preflight" else "historical"
            )
            category = "validations" if args.kind == "preflight" else "historical"
            directory = (
                ROOT
                / "evidence"
                / category
                / datetime.now(UTC).strftime(prefix + "-%Y%m%dT%H%M%S%fZ")
            )
        initialize(args.kind, directory, args.preflight)
    if directory is None:
        raise ValueError("Resume requires directory")
    print("Evidence: " + str(directory), flush=True)
    try:
        result = execute(directory)
        from study.campaign_report import report

        report(directory)
        record_path = ROOT / ".local/study-record.json"
        if record_path.exists():
            with FileLock(str(ROOT / ".local/study-record.lock"), timeout=10):
                record = read(record_path)
                record["current_execution"] = {
                    "directory": str(directory),
                    "kind": read(directory / "manifest.json")["kind"],
                    "status": result["status"],
                    "planned": result["planned"],
                    "scored": result["scored"],
                    "human_review": "pending",
                    "report": str(directory / "analysis/report.md"),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                record["events"].append(
                    {
                        "date": datetime.now(UTC).isoformat(),
                        "event": "Campaign stage ended; offline evidence audit completed",
                        **record["current_execution"],
                    }
                )
                save(record_path, record)
    except Exception as exc:  # noqa: BLE001 - suppress credential-bearing error text
        save(
            directory / "execution-error.json",
            {
                "error_type": type(exc).__name__,
                "at": time.time(),
                "note": "Execution stopped; no automatic replay",
            },
        )
        print(
            "Campaign stopped: " + type(exc).__name__ + "; inspect saved evidence",
            flush=True,
        )
        return 1
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
