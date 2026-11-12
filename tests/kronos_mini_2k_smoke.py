"""Offline mini/2k model smoke, runnable without pytest in the runtime image.

From the repository root, run ``python -m tests.kronos_mini_2k_smoke``.
The artifact paths use the same environment variables as the application.
For a Docker run, mount this file read-only and use it as the Python script.
"""

from __future__ import annotations

import hashlib
import math
import sys
import time
from pathlib import Path

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast import sampler as sampler_module
from tradingagents.forecast.artifact import (
    MIN_PATHS,
    isolated_model_dir,
    isolated_python,
    isolated_source_dir,
    profile_for,
    verify_artifacts,
)
from tradingagents.forecast.sampler import KronosSampler, SamplerSettings


class MissingArtifacts(RuntimeError):
    """A pinned file or the isolated interpreter is not installed."""


def run_smoke() -> None:
    source, models = isolated_source_dir(), isolated_model_dir()
    problem, verified = verify_artifacts(source, models, "mini_2k")
    if problem is not None and " is missing:" in problem:
        raise MissingArtifacts(problem)
    if problem is not None:
        raise AssertionError(problem)
    if not isolated_python().is_file():
        raise MissingArtifacts("the isolated Kronos interpreter is missing")
    profile = profile_for("mini_2k")
    assert verified == {**profile.source_hashes, **profile.model_hashes}
    assert profile.source_revision == "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
    assert profile.model_revision == "f4e68697d9d5aed55cef5c96aabc3376bcad9f81"
    assert profile.tokenizer_revision == "26966d0035065a0cae0ebad7af8ece35bc1fb51c"
    assert profile.max_context == 2048
    assert MIN_PATHS == 16
    sampler_path = Path(sampler_module.__file__)
    sampler_hash = hashlib.sha256(sampler_path.read_bytes()).hexdigest()
    print(f"sampler_module={sampler_path} sha256={sampler_hash}", flush=True)
    print(
        f"verified {len(verified)} source/config/weight hashes; "
        f"source_revision={profile.source_revision} "
        f"model_revision={profile.model_revision} "
        f"tokenizer_revision={profile.tokenizer_revision}",
        flush=True,
    )

    step = 14_400_000
    cutoff = 1_735_689_600_000
    candles = tuple(
        Candle(
            symbol="BTC/USDT:USDT", timeframe="4h",
            open_time=cutoff - step * (2048 - index),
            close_time=cutoff - step * (2047 - index),
            open=42_000.0 + index * 0.05 + math.sin(index / 7),
            high=42_003.0 + index * 0.05 + math.sin(index / 7),
            low=41_997.0 + index * 0.05 + math.sin(index / 7),
            close=42_000.2 + index * 0.05 + math.sin(index / 7),
            volume=100.0 + index % 10, is_closed=True,
        )
        for index in range(2048)
    )
    sampler = KronosSampler(
        python=isolated_python(), source_dir=source, model_dir=models,
        settings=SamplerSettings(profile="mini_2k", paths=16, temperature=0.5),
    )
    process = None
    try:
        started = time.perf_counter()
        cold = sampler.sample([("BTC/USDT:USDT", "4h", 24, candles, 4242)])[0]
        cold_seconds = time.perf_counter() - started
        process = sampler._process
        assert cold.status == "ok", cold.reason
        assert process is not None and process.poll() is None
        print(f"cold={cold_seconds:.2f}s child_pid={process.pid}", flush=True)
        started = time.perf_counter()
        warm = sampler.sample([("BTC/USDT:USDT", "4h", 24, candles, 4242)])[0]
        warm_seconds = time.perf_counter() - started
        assert warm.status == "ok", warm.reason
        assert sampler._process is process and process.poll() is None
        for result in (cold, warm):
            assert result.params["profile"] == "mini_2k"
            assert len(result.paths) == 16
            assert all(
                len(path[field]) == 6
                for path in result.paths
                for field in ("opens", "highs", "lows", "closes")
            )
        print(f"warm={warm_seconds:.2f}s paths=16 candles_per_path=6", flush=True)
        try:
            status = Path(f"/proc/{process.pid}/status").read_text(encoding="utf-8")
            peak = next(line for line in status.splitlines() if line.startswith("VmHWM:"))
        except (OSError, StopIteration):
            peak = "unavailable"
        print(f"child_peak_rss={peak}", flush=True)
    finally:
        sampler.close()
    assert process is not None and process.poll() is not None
    print(f"child_cleanup=exited returncode={process.returncode}", flush=True)


if __name__ == "__main__":
    try:
        run_smoke()
    except MissingArtifacts as exc:
        print(f"smoke unavailable: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
