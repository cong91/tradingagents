"""Forecast SAMPLER: the isolated child's lifecycle and its answer contract.

Unit tests for the failures reproduced in docs/research/kronos-active-review-
20261111.vi.md and the seams around them:

- ``close`` once cleared ``self._process`` BEFORE calling ``kill()``, so the
  kill re-read a now-None attribute and left the warm model child alive after
  every close — the captured-process regression is pinned with a fake Popen;
- a real child that ignores EOF must still be killed by ``close``;
- a chunk deadline must kill the child (a late answer may never be read as the
  NEXT request's result) and reap it;
- the parent matches answers by request id: model banner lines and stale lines
  are skipped, a malformed answer is an error WITHOUT desynchronizing a warm
  child (the child survives and answers the next request);
- a result is trusted only when it is exactly what was requested: the symbol
  echoes, the path count matches the request, every path covers the horizon,
  prices are finite and positive, and per-path seeds are distinct.

The stub runner runs under the repo interpreter with ``-I``; no model, no
network, no database.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.sampler import (
    DEFAULT_CHUNK_DEADLINE_SECONDS,
    MAX_BATCH_ROWS,
    KronosSampler,
    SampledSymbol,
    SamplerError,
    SamplerSettings,
    plan_batches,
    validate_settings,
)

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000

_STUB_BODY = '''
import json, sys, time

MODE = {mode!r}

def _paths(base_seed, count, pred):
    return [
        {{
            "opens": [100.0] * pred, "closes": [100.0] * pred,
            "highs": [101.0] * pred, "lows": [99.0] * pred,
            "seed": base_seed + i,
        }}
        for i in range(count)
    ]

def main():
    if MODE == "hang":
        sys.stdin.readline()
        time.sleep(3600)
        return 0
    if MODE == "no_read":
        time.sleep(3600)
        return 0
    if MODE == "exit_without_answer":
        sys.stdin.readline()
        return 3
    if MODE == "ignore_eof":
        sys.stdin.readline()
        time.sleep(3600)
        return 0
    seen = 0
    for line in sys.stdin:
        if not line.strip():
            break
        payload = json.loads(line)
        rid = payload.get("request_id")
        count = payload.get("paths", 16)
        pred = payload.get("pred_len", 2)
        seeds = payload.get("seeds", [])
        series = payload.get("requests", [])
        metadata = {{
            "profile": payload.get("profile", "small_base"),
            "model_repo": payload.get("model_repo", "NeoQuasar/Kronos-small"),
            "tokenizer_repo": payload.get("tokenizer_repo", "NeoQuasar/Kronos-Tokenizer-base"),
            "model_revision": payload.get("model_revision", "901c26c1332695a2a8f243eb2f37243a37bea320"),
            "tokenizer_revision": payload.get("tokenizer_revision", "0e0117387f39004a9016484a186a908917e22426"),
            "max_context": payload.get("max_context", 512),
        }}
        system = {{
            "model_sha256": payload.get("model_sha256", "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"),
            "tokenizer_sha256": payload.get("tokenizer_sha256", "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"),
            "model_config_sha256": payload.get("model_config_sha256", "5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396"),
            "tokenizer_config_sha256": payload.get("tokenizer_config_sha256", "2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2"),
        }}
        if MODE == "banner":
            print("Loading weights from local directory", flush=True)
        if MODE == "stale_then_answer" and seen == 0:
            print(json.dumps({{"request_id": (rid or 0) + 999, "paths": []}}), flush=True)
        if MODE == "malformed_once" and seen == 0:
            count = 1
        if MODE == "one_path":
            count = 1
        if MODE == "short_horizon":
            pred = 1
        if MODE == "dup_seeds":
            rows = []
            for ri, r in enumerate(series):
                paths = _paths(seeds[ri], count, pred)
                for path in paths:
                    path["seed"] = 42  # NOT distinct: the draws are not independent
                rows.append({{"symbol": r["symbol"], "paths": paths}})
            print(json.dumps({{"request_id": rid, "paths": rows, "metadata": metadata, "system": system}}), flush=True)
            seen += 1
            continue
        if MODE == "nan_price":
            answer = {{"request_id": rid, "paths": [], "metadata": metadata, "system": system}}
            rows = []
            for ri, r in enumerate(series):
                paths = _paths(seeds[ri], count, pred)
                paths[0]["closes"][0] = float("nan")
                rows.append({{"symbol": r["symbol"], "paths": paths}})
            answer["paths"] = rows
            print(json.dumps(answer), flush=True)
            seen += 1
            continue
        if MODE in ("missing_opens", "nan_open", "bad_ohlc", "inverted_high_low", "huge_open", "wrong_seed"):
            rows = []
            for ri, r in enumerate(series):
                paths = _paths(seeds[ri], count, pred)
                if MODE == "missing_opens":
                    del paths[0]["opens"]
                elif MODE == "nan_open":
                    paths[0]["opens"][0] = float("nan")
                elif MODE == "bad_ohlc":
                    paths[0]["opens"][0] = 102.0
                    paths[0]["closes"][1] = 102.0
                    paths[1]["closes"][0] = 98.0
                elif MODE == "inverted_high_low":
                    paths[0]["highs"][0] = 98.0
                elif MODE == "huge_open" and ri == 0:
                    paths[0]["opens"][0] = 10 ** 400
                elif MODE == "wrong_seed":
                    paths[0]["seed"] = seeds[ri] + count
                rows.append({{"symbol": r["symbol"], "paths": paths}})
            print(json.dumps({{"request_id": rid, "paths": rows, "metadata": metadata, "system": system}}), flush=True)
            seen += 1
            continue
        if MODE == "wrong_symbol":
            answer = {{"request_id": rid, "paths": [
                {{"symbol": "ETH/USDT:USDT", "paths": _paths(seeds[ri], count, pred)}}
                for ri, r in enumerate(series)
            ], "metadata": metadata, "system": system}}
            print(json.dumps(answer), flush=True)
            seen += 1
            continue
        answer = {{"request_id": rid, "paths": [
            {{"symbol": r["symbol"], "paths": _paths(seeds[ri], count, pred)}}
            for ri, r in enumerate(series)
        ], "metadata": metadata, "system": system}}
        print(json.dumps(answer), flush=True)
        seen += 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def _write_stub(tmp_path: Path, mode: str) -> Path:
    stub = tmp_path / f"stub_{mode}.py"
    stub.write_text(_STUB_BODY.format(mode=mode), encoding="utf-8")
    return stub


def _candles(count: int = 40, step: int = _FOUR_HOUR_MS, *,
             symbol: str = "BTC/USDT:USDT", timeframe: str = "4h") -> tuple[Candle, ...]:
    last = 1_800_000_000_000 - (1_800_000_000_000 % step)
    opens = [last - step * (count - 1 - index) for index in range(count)]
    return tuple(
        Candle(
            symbol=symbol, timeframe=timeframe, open_time=open_time,
            close_time=open_time + step, open=99.5, high=101.0, low=99.0,
            close=100.0, volume=10.0, is_closed=True,
        )
        for open_time in opens
    )


def _request(**overrides) -> tuple:
    base = ("BTC/USDT:USDT", "4h", 8, _candles(), 4242)
    return tuple(base[index] if index != 2 else overrides.get("horizon_hours", 8)
                 for index in range(5))


def _settings(**overrides) -> SamplerSettings:
    base = {"paths": 16, "chunk_deadline_seconds": 10.0}
    base.update(overrides)
    return SamplerSettings(**base)


def _sampler(tmp_path: Path, mode: str, **settings) -> KronosSampler:
    return KronosSampler(
        python=Path(sys.executable), source_dir=tmp_path / "src",
        model_dir=tmp_path / "models", runner=_write_stub(tmp_path, mode),
        settings=_settings(**settings),
    )


# -- the close/kill capture bug ----------------------------------------------


class _FakeStream(io.StringIO):
    def close(self) -> None:  # record that the parent closed the owned pipe
        self.closed_by_parent = True
        super().close()


class _FakeProcess:
    """A Popen stand-in precise enough to pin the close/kill call ordering."""

    def __init__(self, *, graceful_wait_times_out: bool) -> None:
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.killed = False
        self.reaped = False
        self._graceful_wait_times_out = graceful_wait_times_out

    def poll(self):
        return 0 if self.reaped else None

    def wait(self, timeout=None):
        if self.killed or not self._graceful_wait_times_out:
            self.reaped = True
            return 0
        raise subprocess.TimeoutExpired("stub", timeout)

    def kill(self):
        self.killed = True


def test_close_uses_the_captured_process_so_the_kill_still_runs():
    """The documented bug: close cleared ``self._process`` first, so the
    except-branch ``kill()`` re-read None and the child survived."""
    process = _FakeProcess(graceful_wait_times_out=True)
    sampler = KronosSampler(python="py", source_dir="s", model_dir="m")
    sampler._process = process
    sampler.close()
    assert process.killed, "a child that ignores graceful close must still be killed"
    assert process.reaped, "the killed child must be waited on (reaped)"
    assert process.stdin.closed_by_parent and process.stdout.closed_by_parent


def test_close_signals_a_cooperative_child_through_stdin_eof():
    process = _FakeProcess(graceful_wait_times_out=False)
    sampler = KronosSampler(python="py", source_dir="s", model_dir="m")
    sampler._process = process
    sampler.close()
    assert not process.killed, "a child that exits on stdin EOF must not be killed"
    assert process.reaped
    assert process.stdin.closed_by_parent


def test_kill_is_safe_when_the_child_was_never_started():
    sampler = KronosSampler(python="py", source_dir="s", model_dir="m")
    sampler.close()
    sampler.kill()


# -- real child lifecycle -----------------------------------------------------


class _RecordingPopen(subprocess.Popen):
    instances: list[subprocess.Popen] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _RecordingPopen.instances.append(self)


def test_a_chunk_deadline_kills_the_real_child_and_reports_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingagents.forecast.sampler.subprocess.Popen", _RecordingPopen)
    _RecordingPopen.instances.clear()
    sampler = _sampler(tmp_path, "hang", chunk_deadline_seconds=0.3)
    started = time.monotonic()
    results = sampler.sample([_request()])
    elapsed = time.monotonic() - started
    assert results[0].status == "timeout"
    assert "deadline" in (results[0].reason or "")
    assert elapsed < 30, "the deadline must bound the wait, not hang the caller"
    assert _RecordingPopen.instances, "the stub child was never started"
    child = _RecordingPopen.instances[-1]
    sampler.close()
    assert child.poll() is not None, "cleanup must reap the timed-out child"


def test_close_ends_a_warm_child(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingagents.forecast.sampler.subprocess.Popen", _RecordingPopen)
    _RecordingPopen.instances.clear()
    sampler = _sampler(tmp_path, "ok")
    results = sampler.sample([_request()])
    assert results[0].status == "ok"
    child = _RecordingPopen.instances[-1]
    assert child.poll() is None, "the child is warm between requests"
    sampler.close()
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child.poll() == 0, "close must end the owned child"


def test_sequential_requests_reuse_one_actual_child(tmp_path):
    sampler = _sampler(tmp_path, "ok")
    try:
        first = sampler.sample([_request()], deadline=time.monotonic() + 10)
        child = sampler._process
        request = list(_request())
        request[-1] = 9000
        second = sampler.sample([tuple(request)], deadline=time.monotonic() + 20)
        assert first[0].status == second[0].status == "ok"
        assert sampler._process is child and child.poll() is None
        assert sampler._request_counter == 2
        assert [path["seed"] for path in first[0].paths] == list(range(4242, 4258))
        assert [path["seed"] for path in second[0].paths] == list(range(9000, 9016))
    finally:
        sampler.close()
    assert child.poll() == 0


def test_timed_out_child_is_reaped_and_next_request_starts_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingagents.forecast.sampler.subprocess.Popen", _RecordingPopen)
    _RecordingPopen.instances.clear()
    sampler = _sampler(tmp_path, "hang")
    try:
        first = sampler.sample([_request()], deadline=time.monotonic() + 0.2)
        assert first[0].status == "timeout"
        old_child = _RecordingPopen.instances[-1]
        assert sampler._process is None
        assert sampler._io_thread is None
        sampler._runner = _write_stub(tmp_path, "ok")
        second = sampler.sample([_request()], deadline=time.monotonic() + 10)
        assert second[0].status == "ok", second[0].reason
        assert old_child.poll() is not None
        assert all(stream.closed for stream in (old_child.stdin, old_child.stdout, old_child.stderr))
        assert len(_RecordingPopen.instances) == 2
        assert sampler._process is _RecordingPopen.instances[-1]
        assert sampler._process is not old_child
        assert sampler._request_counter == 2
        assert sampler.settings.chunk_deadline_seconds == 10
    finally:
        sampler.close()
    assert all(child.poll() is not None for child in _RecordingPopen.instances)


def test_sample_lock_wait_expires_without_disturbing_the_active_child(tmp_path):
    sampler = _sampler(tmp_path, "ok")
    outcomes = []
    assert sampler.sample([_request()])[0].status == "ok"
    child = sampler._process

    def sample():
        outcomes.extend(sampler.sample([_request()], deadline=time.monotonic() + 0.1))

    worker = threading.Thread(target=sample)
    try:
        with sampler._lock:
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive(), "sampler lock wait must honor the request deadline"
            assert len(outcomes) == 1 and outcomes[0].status == "timeout"
            assert sampler._request_counter == 1
            assert sampler._process is child and child.poll() is None
        assert sampler.sample([_request()], deadline=time.monotonic() + 10)[0].status == "ok"
        assert sampler._process is child
    finally:
        worker.join(timeout=2)
        sampler.close()


def test_process_construction_cannot_rebase_the_absolute_deadline(tmp_path, monkeypatch):
    sampler = _sampler(tmp_path, "ok")
    clock = [100.0]
    ensure_process = sampler._ensure_process
    monkeypatch.setattr("tradingagents.forecast.sampler.time.monotonic", lambda: clock[0])

    def slow_start(*, deadline=None):
        process = ensure_process(deadline=deadline)
        clock[0] = 151.0
        return process

    monkeypatch.setattr(sampler, "_ensure_process", slow_start)
    try:
        results = sampler.sample([_request()], deadline=150.0)
        assert results[0].status == "timeout"
        assert sampler._process is None
    finally:
        sampler.close()


def test_no_child_is_started_for_an_already_expired_request(tmp_path, monkeypatch):
    sampler = _sampler(tmp_path, "ok")

    def no_process(*, deadline=None):
        pytest.fail("an expired request must not start a child")

    monkeypatch.setattr(sampler, "_ensure_process", no_process)
    results = sampler.sample([_request()], deadline=time.monotonic() - 1)
    assert results[0].status == "timeout"
    assert sampler._request_counter == 0


@pytest.mark.parametrize("problem", [None, "hash mismatch"])
def test_preflight_expiring_the_deadline_never_starts_a_child(tmp_path, monkeypatch, problem):
    clock = [100.0]
    starts = []
    sampler = _sampler(tmp_path, "ok", chunk_deadline_seconds=10)

    def slow_verifier():
        clock[0] = 106.0
        return problem

    sampler._artifact_verifier = slow_verifier
    monkeypatch.setattr("tradingagents.forecast.sampler.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "tradingagents.forecast.sampler.subprocess.Popen",
        lambda *_args, **_kwargs: starts.append(True),
    )
    result = sampler.sample([_request()], deadline=105.0)[0]
    assert result.status == "timeout"
    assert starts == []
    assert sampler._process is None


@pytest.mark.parametrize("outcome", ["complete", "expired", "failed"])
def test_next_chunk_waits_for_prior_timeout_cleanup(tmp_path, monkeypatch, outcome):
    sampler = _sampler(tmp_path, "ok", max_batch_rows=16)
    first = _FakeProcess(graceful_wait_times_out=True)
    second = _FakeProcess(graceful_wait_times_out=True)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    calls = []
    results = []
    deadline = time.monotonic() + (0.3 if outcome == "expired" else 5)

    class PendingCleanup:
        def join(self, timeout=None):
            cleanup_started.set()
            assert release_cleanup.wait(2), "the test must release the cleanup wait"

        def is_alive(self):
            return outcome == "expired"

    def ensure_process(*, deadline=None):
        calls.append("start")
        assert len(calls) == 1 or cleanup_finished.is_set()
        sampler._process = first if len(calls) == 1 else second
        return sampler._process

    def communicate(_process, _payload, *, deadline=None):
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("stub", 1)
        return {"error": "second chunk reached the child"}

    def defer_kill():
        sampler._process = None
        sampler._cleanup_thread = PendingCleanup()
        if outcome == "failed":
            sampler._cleanup_error = SamplerError("cleanup failed")
        if outcome == "complete":
            cleanup_finished.set()

    monkeypatch.setattr(sampler, "_ensure_process", ensure_process)
    monkeypatch.setattr(sampler, "_communicate", communicate)
    monkeypatch.setattr(sampler, "_defer_kill", defer_kill)
    worker = threading.Thread(target=lambda: results.extend(sampler.sample(
        [_request(), _request()], deadline=deadline,
    )))
    worker.start()
    try:
        assert cleanup_started.wait(1), "the next chunk must wait for cleanup"
        assert calls == ["start"], "a replacement cannot start while cleanup is pending"
    finally:
        if outcome == "expired":
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
        release_cleanup.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    expected = {"complete": "error", "expired": "timeout", "failed": "unavailable"}
    assert [result.status for result in results] == ["timeout", expected[outcome]]
    assert len(calls) == (2 if outcome == "complete" else 1)


def test_failed_deferred_cleanup_can_be_retried_without_losing_child(tmp_path):
    sampler = _sampler(tmp_path, "ok")

    class RetryProcess(_FakeProcess):
        def __init__(self):
            super().__init__(graceful_wait_times_out=True)
            self.failed_once = False

        def wait(self, timeout=None):
            if timeout == 0 or not self.failed_once:
                if timeout != 0:
                    self.failed_once = True
                raise subprocess.TimeoutExpired("stub", timeout)
            return super().wait(timeout=timeout)

    process = RetryProcess()
    sampler._process = process
    sampler._defer_kill()
    sampler._cleanup_thread.join(timeout=2)
    assert sampler._cleanup_error is not None
    assert sampler._cleanup_process is process

    sampler.close()

    assert sampler._cleanup_error is None
    assert sampler._cleanup_process is None
    assert process.reaped
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_timeout_does_not_wait_for_a_blocked_stream_close(tmp_path):
    sampler = _sampler(tmp_path, "ok")
    process = _FakeProcess(graceful_wait_times_out=True)
    close_started = threading.Event()
    release_close = threading.Event()
    returned = threading.Event()

    class BlockingClose(_FakeStream):
        def close(self):
            close_started.set()
            assert release_close.wait(2), "the test must release the blocked close"
            super().close()

    process.stdout = BlockingClose()
    sampler._process = process
    worker = threading.Thread(target=lambda: (sampler._defer_kill(), returned.set()))
    worker.start()
    try:
        assert close_started.wait(1)
        assert returned.wait(0.3), "closing a pipe must not block the deadline path"
        assert sampler._cleanup_process is process
    finally:
        release_close.set()
        worker.join(timeout=2)
        sampler.close()
    assert not worker.is_alive()
    assert process.reaped
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_a_large_payload_to_a_child_that_never_reads_is_bounded(tmp_path):
    stub = _write_stub(tmp_path, "no_read")
    probe = tmp_path / "bounded_pipe_probe.py"
    probe.write_text(
        "import json, pathlib, sys, threading, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from tradingagents.forecast.sampler import KronosSampler, SamplerSettings\n"
        "s = KronosSampler(python=sys.executable, source_dir='s', model_dir='m', "
        "runner=sys.argv[2], settings=SamplerSettings(chunk_deadline_seconds=0.3))\n"
        "p = s._ensure_process()\n"
        "threading.Timer(4.0, p.kill).start()\n"
        "started = time.monotonic()\n"
        "try:\n"
        "    s._communicate(p, {'request_id': 1, 'large': 'x' * 1000000})\n"
        "except Exception as exc:\n"
        "    error = type(exc).__name__\n"
        "else:\n"
        "    error = 'no timeout'\n"
        "elapsed = time.monotonic() - started\n"
        "s.close()\n"
        "print(json.dumps({'error': error, 'elapsed': elapsed, 'dead': p.poll() is not None, "
        "'closed': all(v.closed for v in (p.stdin,p.stdout,p.stderr)), "
        "'workers': [t.name for t in threading.enumerate() if t.name.startswith('kronos-sampler')]}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-I", str(probe), str(Path(__file__).resolve().parents[1]), str(stub)],
        capture_output=True, encoding="utf-8", timeout=12, check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["error"] == "TimeoutExpired", evidence
    assert evidence["elapsed"] < 2, evidence
    assert evidence["dead"] and evidence["closed"], evidence
    assert evidence["workers"] == [], evidence


def test_partial_writer_budget_leaves_only_the_absolute_deadline_for_reading(tmp_path):
    sampler = _sampler(tmp_path, "hang", chunk_deadline_seconds=0.25)
    process = sampler._ensure_process()
    original = process.stdin

    class DelayedInput:
        def write(self, text):
            time.sleep(0.16)
            return original.write(text)

        def flush(self):
            return original.flush()

        def close(self):
            return original.close()

    process.stdin = DelayedInput()
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            sampler._communicate(process, {"request_id": 1})
        assert time.monotonic() - started < 0.55
        assert sampler._io_thread is None
    finally:
        sampler.close()
    assert process.poll() is not None


# -- the answer protocol -------------------------------------------------------


def test_banner_lines_are_skipped_and_the_answer_matched_by_id(tmp_path):
    sampler = _sampler(tmp_path, "banner")
    results = sampler.sample([_request()])
    assert results[0].status == "ok", results[0].reason


def test_a_stale_answer_line_is_skipped_not_trusted(tmp_path):
    """A line without THIS request's id is consumed and skipped, so the stream
    resynchronizes instead of reading someone else's answer as a result."""
    sampler = _sampler(tmp_path, "stale_then_answer")
    results = sampler.sample([_request()])
    assert results[0].status == "ok", results[0].reason


def test_a_malformed_answer_is_an_error_that_keeps_the_child_in_sync(tmp_path):
    """A bad CONTENT answer (wrong path count) is an error for that chunk, but
    the child consumed exactly one line per request, so it stays warm and the
    NEXT request is answered — no desync, no kill."""
    sampler = _sampler(tmp_path, "malformed_once")
    first = sampler.sample([_request()])
    assert first[0].status == "error"
    assert "1 path(s) for a 16-path request" in (first[0].reason or "")
    second = sampler.sample([_request()])
    assert second[0].status == "ok", second[0].reason
    sampler.close()


def test_an_absent_answer_is_an_error_and_the_child_is_stopped(tmp_path):
    sampler = _sampler(tmp_path, "exit_without_answer")
    results = sampler.sample([_request()])
    assert results[0].status == "error"
    assert "stdout" in (results[0].reason or "")


# -- the answer contract -------------------------------------------------------


@pytest.mark.parametrize("mode,needle", [
    ("one_path", "1 path(s) for a 16-path request"),
    ("short_horizon", "horizon"),
    ("nan_price", "non-finite"),
    ("missing_opens", "opens"),
    ("nan_open", "non-finite"),
    ("wrong_seed", "seeds"),
    ("wrong_symbol", "not the requested"),
    ("dup_seeds", "seeds"),
])
def test_malformed_answers_are_refused_with_the_concrete_reason(tmp_path, mode, needle):
    sampler = _sampler(tmp_path, mode)
    results = sampler.sample([_request()])
    assert results[0].status == "error"
    assert needle in (results[0].reason or "")


def test_inconsistent_open_close_order_preserves_raw_values_and_audits_correction(tmp_path):
    sampler = _sampler(tmp_path, "bad_ohlc")
    try:
        entry = sampler.sample([_request()])[0]
    finally:
        sampler.close()
    assert entry.status == "ok", entry.reason
    assert len(entry.paths) == 16
    path = entry.paths[0]
    assert path["opens"] == [102.0, 100.0]
    assert path["closes"] == [100.0, 102.0]
    assert path["raw_ohlc"]["highs"] == [101.0, 101.0]
    assert path["highs"] == [102.0, 102.0]
    assert path["ohlc_provenance"]["corrected_bar_count"] == 2
    assert entry.ohlc_quality["status"] == "corrected"
    assert entry.paths[1]["closes"] == [98.0, 100.0]


def test_inverted_high_low_is_corrected_and_raw_bounds_are_retained(tmp_path):
    sampler = _sampler(tmp_path, "inverted_high_low")
    try:
        entry = sampler.sample([_request()])[0]
    finally:
        sampler.close()
    assert entry.status == "ok", entry.reason
    path = entry.paths[0]
    assert path["raw_ohlc"]["highs"][0] == 98.0
    assert path["raw_ohlc"]["lows"][0] == 99.0
    assert path["highs"][0] == 100.0
    assert path["lows"][0] == 99.0
    assert path["ohlc_provenance"]["corrected_bar_count"] == 1


def test_overflowing_price_fails_only_its_symbol_in_a_batch(tmp_path):
    sampler = _sampler(tmp_path, "huge_open")
    first = _request()
    second = ("ETH/USDT:USDT", first[1], first[2], _candles(symbol="ETH/USDT:USDT"), 5000)
    try:
        bad, good = sampler.sample([first, second])
    finally:
        sampler.close()
    assert bad.status == "error" and "opens" in (bad.reason or "")
    assert good.status == "ok", good.reason
    assert good.symbol == "ETH/USDT:USDT"
    assert [path["seed"] for path in good.paths] == list(range(5000, 5016))


def test_a_correct_answer_carries_the_drawn_paths(tmp_path):
    sampler = _sampler(tmp_path, "ok")
    results = sampler.sample([_request(horizon_hours=8)])
    entry = results[0]
    assert entry.status == "ok"
    assert len(entry.paths) == 16
    assert all(len(path["closes"]) == 2 for path in entry.paths), "8h/4h = 2 bars"
    assert all(path["opens"] == [100.0, 100.0] for path in entry.paths)
    assert entry.params["paths"] == 16
    assert {path["seed"] for path in entry.paths} == {4242 + i for i in range(16)}, (
        "per-path seeds are base + index: draws independent of batch order"
    )


def test_response_rejects_a_changed_pinned_config_hash():
    from tradingagents.forecast.artifact import profile_for
    from tradingagents.forecast.sampler import _response_metadata_problem

    profile = profile_for("small_base")
    system = {
        "model_sha256": profile.model_hashes["Kronos-small/model.safetensors"],
        "tokenizer_sha256": profile.model_hashes["Kronos-Tokenizer-base/model.safetensors"],
        "model_config_sha256": "changed-config",
        "tokenizer_config_sha256": profile.model_hashes["Kronos-Tokenizer-base/config.json"],
    }
    metadata = {
        "profile": profile.name,
        "model_repo": profile.model_repo,
        "tokenizer_repo": profile.tokenizer_repo,
        "model_revision": profile.model_revision,
        "tokenizer_revision": profile.tokenizer_revision,
        "max_context": profile.max_context,
    }
    problem = _response_metadata_problem({"metadata": metadata, "system": system}, profile)
    assert problem is not None
    assert "model_config_sha256" in problem


def test_from_config_refuses_an_invalid_profile_before_artifact_verification(monkeypatch):
    from tradingagents.forecast.service import KronosForecastService

    service = KronosForecastService.from_config({
        "kronos_active_profile": "invalid-profile",
    })
    assert service._sampler is None
    assert service.artifact_hash is None
    assert "unknown Kronos profile" in (service.artifact_problem or "")


# -- nothing spawns before the pinned artifacts verify ------------------------


def test_from_config_refuses_before_spawning_when_verification_fails(monkeypatch, tmp_path):
    """The preflight must surface an unavailable/hash mismatch BEFORE any child
    exists — a swapped weight file must never be imported or loaded."""
    from datetime import UTC, datetime

    from tradingagents.forecast import artifact as artifact_module
    from tradingagents.forecast.block import CandleSeries
    from tradingagents.forecast.service import KronosForecastService

    monkeypatch.setattr(artifact_module, "isolated_python", lambda: Path(sys.executable))
    monkeypatch.setattr(artifact_module, "isolated_source_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(artifact_module, "isolated_model_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(
        artifact_module,
        "verify_artifacts",
        lambda _source, _model, _profile: (
            "pinned Kronos weight hash mismatch for Kronos-small", {}
        ),
    )
    service = KronosForecastService.from_config({})
    assert service.artifact_problem is not None
    assert service.artifact_hash is None, (
        "an expected hash must never be labelled as one actually checked"
    )
    assert service._sampler is None, "no sampler exists to spawn a child with"
    candles = _candles()
    block = service.forecast(
        CandleSeries(symbol="BTC/USDT:USDT", candles=candles,
                     as_of=datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)),
        seed=1,
    )
    assert block.status == "unavailable"
    assert "hash mismatch" in (block.reason or "")


def test_from_config_records_the_verified_digest_when_artifacts_match(monkeypatch, tmp_path):
    """A verified run records the digest of what was ACTUALLY checked."""
    from tradingagents.forecast import artifact as artifact_module
    from tradingagents.forecast.service import KronosForecastService

    monkeypatch.setattr(artifact_module, "isolated_python", lambda: Path(sys.executable))
    monkeypatch.setattr(artifact_module, "isolated_source_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(artifact_module, "isolated_model_dir", lambda: tmp_path / "models")

    def _verify(_source, _model, _profile):
        verified = {**artifact_module.PINNED_SOURCE_HASHES, **artifact_module.PINNED_WEIGHT_HASHES}
        return None, verified

    monkeypatch.setattr(artifact_module, "verify_artifacts", _verify)
    service = KronosForecastService.from_config({
        "exec_exchange_id": "binance",
    })
    assert service.artifact_problem is None
    assert service.artifact_hash == artifact_module.artifact_digest_from(
        {**artifact_module.PINNED_SOURCE_HASHES, **artifact_module.PINNED_WEIGHT_HASHES}
    )
    assert service._sampler is not None
    assert service.venue == "binance"


def test_each_restarted_production_child_reverifies_artifacts_but_warm_reuse_does_not(
    monkeypatch, tmp_path,
):
    """A changed source is refused before the replacement child can import it."""
    verifier_calls = []
    source_file = tmp_path / "pinned_source.py"
    source_file.write_text("pinned", encoding="utf-8")
    starts = []

    class _Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def kill(self):
            self.alive = False

        def wait(self, timeout=None):
            self.alive = False
            return 0

    def verify():
        verifier_calls.append(True)
        return (
            None
            if source_file.read_text(encoding="utf-8") == "pinned"
            else "pinned Kronos source hash mismatch"
        )

    def start(*_args, **_kwargs):
        starts.append(True)
        return _Process()

    sampler = KronosSampler(
        python=Path(sys.executable), source_dir=tmp_path / "src",
        model_dir=tmp_path / "models", settings=_settings(),
        artifact_verifier=verify,
    )
    monkeypatch.setattr("tradingagents.forecast.sampler.subprocess.Popen", start)
    first = sampler._ensure_process()
    assert first is not None
    assert sampler._ensure_process() is first
    assert verifier_calls == [True], "a warm child does not reverify each request"
    sampler.kill()
    source_file.write_text("altered", encoding="utf-8")
    with pytest.raises(SamplerError, match="source hash mismatch"):
        sampler._ensure_process()
    assert len(starts) == 1, "the altered source must fail before a replacement spawn"
    assert verifier_calls == [True, True]


@pytest.mark.parametrize("entrypoint", ["direct", "service"])
@pytest.mark.parametrize("tampered", ["source", "model"])
def test_same_metadata_tampering_is_detected_before_each_production_spawn(
    monkeypatch, tmp_path, entrypoint, tampered,
):
    from tradingagents.forecast import artifact as artifact_module
    from tradingagents.forecast.service import KronosForecastService

    source_dir, model_dir = tmp_path / "src", tmp_path / "models"
    source_dir.mkdir()
    model_dir.mkdir()
    source = source_dir / "source.py"
    model = model_dir / "weights.bin"
    source.write_bytes(b"pinned source")
    model.write_bytes(b"pinned weight")
    selected = replace(
        artifact_module.profile_for(),
        source_hashes={source.name: hashlib.sha256(source.read_bytes()).hexdigest()},
        model_hashes={model.name: hashlib.sha256(model.read_bytes()).hexdigest()},
    )
    monkeypatch.setattr(artifact_module, "PROFILES", {selected.name: selected})
    monkeypatch.setattr(artifact_module, "_VERIFY_CACHE", {})
    hashed = []
    sha256 = artifact_module._sha256

    def record_hash(path):
        hashed.append(path)
        return sha256(path)

    monkeypatch.setattr(artifact_module, "_sha256", record_hash)
    monkeypatch.setattr(artifact_module, "isolated_source_dir", lambda: source_dir)
    monkeypatch.setattr(artifact_module, "isolated_model_dir", lambda: model_dir)
    monkeypatch.setattr(artifact_module, "isolated_python", lambda: Path(sys.executable))
    starts = []

    def start(*_args, **_kwargs):
        process = _FakeProcess(graceful_wait_times_out=True)
        starts.append(process)
        return process

    monkeypatch.setattr("tradingagents.forecast.sampler.subprocess.Popen", start)
    if entrypoint == "service":
        service = KronosForecastService.from_config({})
        assert service.artifact_problem is None
        sampler = service._sampler
    else:
        assert artifact_module.verify_artifacts(source_dir, model_dir)[0] is None
        sampler = KronosSampler(
            python=Path(sys.executable), source_dir=source_dir, model_dir=model_dir,
            settings=_settings(),
        )
    assert sampler is not None
    assert artifact_module.verify_artifacts(source_dir, model_dir)[0] is None
    assert hashed == [source, model], "ordinary verification retains its cache"
    try:
        first = sampler._ensure_process()
        first_hash_count = len(hashed)
        assert sampler._ensure_process() is first
        assert len(hashed) == first_hash_count, "warm reuse must not hash again"
        sampler.kill()
        path = source if tampered == "source" else model
        stat = path.stat()
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert (path.stat().st_size, path.stat().st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)
        with pytest.raises(SamplerError, match="hash mismatch"):
            sampler._ensure_process()
        assert len(starts) == 1, "tampered artifacts cannot reach a replacement child"
        assert first_hash_count == 4, "each spawn must check actual file contents"
        assert artifact_module.verify_artifacts(source_dir, model_dir)[0] is not None
        path.write_bytes(data)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert artifact_module.verify_artifacts(source_dir, model_dir)[0] is None
    finally:
        sampler.close()


def test_from_config_applies_effective_lookback_override(monkeypatch, tmp_path):
    from tradingagents.forecast import artifact as artifact_module
    from tradingagents.forecast.service import KronosForecastService

    monkeypatch.setattr(artifact_module, "isolated_python", lambda: Path(sys.executable))
    monkeypatch.setattr(artifact_module, "isolated_source_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(artifact_module, "isolated_model_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(
        artifact_module,
        "verify_artifacts",
        lambda _source, _model, _profile: (
            None,
            {**artifact_module.PINNED_SOURCE_HASHES, **artifact_module.PINNED_WEIGHT_HASHES},
        ),
    )

    service = KronosForecastService.from_config(
        {"kronos_active_profile": "mini_2k"}, lookback_bars=201,
    )
    try:
        assert service.settings.profile == "mini_2k"
        assert service.settings.lookback_bars == 201
        assert service._sampler is not None
        assert service._sampler.settings.profile == "mini_2k"
    finally:
        service.close()


# -- settings bounds -----------------------------------------------------------


def test_max_batch_rows_has_a_measured_ceiling():
    assert validate_settings(_settings(paths=16, max_batch_rows=MAX_BATCH_ROWS)) is None
    assert validate_settings(_settings(paths=16, max_batch_rows=MAX_BATCH_ROWS + 1)) is not None
    assert "6.07 GiB" in validate_settings(_settings(paths=16, max_batch_rows=10_000))


def test_batch_rows_must_fit_at_least_one_symbols_paths():
    assert validate_settings(_settings(paths=16, max_batch_rows=15)) is not None
    assert validate_settings(_settings(paths=16, max_batch_rows=16)) is None
    with pytest.raises(RuntimeError, match="at least one"):
        plan_batches(1, max_batch_rows=15, paths=16)
    assert plan_batches(1, max_batch_rows=16, paths=16) == [(0, 1)]


def test_batches_are_bounded_by_rows_not_by_symbol_count():
    assert plan_batches(12, max_batch_rows=80, paths=16) == [(0, 5), (5, 10), (10, 12)]
    assert plan_batches(12, max_batch_rows=80, paths=8) == [(0, 10), (10, 12)]
    assert plan_batches(0, max_batch_rows=80, paths=16) == []


def test_mixed_timeframes_and_horizons_are_split_without_reordering(tmp_path, monkeypatch):
    sampler = _sampler(tmp_path, "ok")
    payloads = []
    communicate = sampler._communicate

    def record_payload(process, payload, *, deadline=None):
        payloads.append(payload)
        return communicate(process, payload, deadline=deadline)

    monkeypatch.setattr(sampler, "_communicate", record_payload)
    requests = [
        ("BTC/USDT:USDT", "4h", 8, _candles(), 1000),
        ("ETH/USDT:USDT", "1h", 2, _candles(step=3_600_000, symbol="ETH/USDT:USDT", timeframe="1h"), 2000),
        ("SOL/USDT:USDT", "4h", 24, _candles(symbol="SOL/USDT:USDT"), 3000),
    ]
    try:
        results = sampler.sample(requests)
    finally:
        sampler.close()
    assert [result.symbol for result in results] == [request[0] for request in requests]
    assert all(result.status == "ok" for result in results)
    assert [len(result.paths[0]["closes"]) for result in results] == [2, 2, 6]
    assert [payload["step_ms"] for payload in payloads] == [_FOUR_HOUR_MS, 3_600_000, _FOUR_HOUR_MS]
    assert [payload["pred_len"] for payload in payloads] == [2, 2, 6]
    assert [result.paths[0]["seed"] for result in results] == [1000, 2000, 3000]


def test_sampler_settings_expose_the_deadline_bound():
    assert _settings().chunk_deadline_seconds == 10.0
    assert SamplerSettings().chunk_deadline_seconds == DEFAULT_CHUNK_DEADLINE_SECONDS


def test_sampled_symbol_carries_the_params_for_the_row():
    symbol = SampledSymbol(symbol="BTC/USDT:USDT", status="ok", params={"paths": 2})
    assert symbol.params == {"paths": 2}
