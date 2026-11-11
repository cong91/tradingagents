"""Bounded local inference: one persistent isolated process, batch requests.

The cost decomposition measured on this machine is what shapes this module:

- ``import torch`` plus the weight load is ~2.4-2.7 s and does not depend on the
  size of the work, so a spawn-per-request shape pays it on EVERY call. For a
  cycle that forecasts several symbols that is minutes spent loading;
- the sampler itself is ~35 ms per bar per path, so the useful work is linear in
  ``bars x paths``;
- one un-chunked batch of 60 symbols x 16 paths peaked at 6.07 GiB RSS on a box
  with 7.2 GiB free. Chunking to 5 symbols per call cost 13% more time and used
  874 MiB instead.

So this client keeps ONE child process warm, sends it one JSON request per
chunk, and never lets a single call exceed a bounded number of rows. The child
is killed and respawned when a request overruns its wall deadline or when the
answer stream can no longer be trusted (EOF, a deadline, or a line limit); a
killed child is reported as a timeout/error, never as a forecast. The process is
started with the operator's isolated interpreter and ``-I``, so a repo-side
import cannot reach into the model and the model's dependencies cannot leak into
the runtime.

Every path is seeded explicitly in the child (``seed + index``). That is what
makes a symbol's forecast independent of the batch it was drawn in, and it is a
precondition for any cache: the model's sampling consumes the global RNG, so
without it the same candles would yield different paths depending on row order.

The 16-path floor is a sampling budget, nothing more: a ratio over N draws is
reported with its counts and an explicit not-a-probability label, and it is NOT
evidence of predictive skill or probability calibration.
"""

from __future__ import annotations

import contextlib
import json
import math
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.artifact import (
    DEFAULT_PROFILE,
    MAX_HORIZON_BARS,
    MAX_PATHS,
    MIN_PATHS,
    RUNNER_PATH,
    TIMEFRAME_HOURS,
    profile_for,
)

# Rows per child request: 5 symbols x 16 paths measured 874 MiB peak against
# 6.07 GiB for the un-chunked 960-row call, for 13% more wall time. 960 rows is
# the hard ceiling — it is the configuration that nearly OOM'd the box.
DEFAULT_MAX_BATCH_ROWS = 80
MAX_BATCH_ROWS = 960
# One chunk's wall deadline. A 5-symbol chunk measured ~17 s warm, so this is
# generous for the work and still bounded well inside a cycle budget.
DEFAULT_CHUNK_DEADLINE_SECONDS = 600.0


class SamplerError(RuntimeError):
    """The child could not be run at all (missing interpreter, bad launch)."""


@dataclass(frozen=True)
class SamplerSettings:
    """The knobs a forecast is drawn under; part of every cache identity."""

    profile: str = DEFAULT_PROFILE
    paths: int = MIN_PATHS
    temperature: float = 0.5
    top_p: float = 0.9
    max_batch_rows: int = DEFAULT_MAX_BATCH_ROWS
    chunk_deadline_seconds: float = DEFAULT_CHUNK_DEADLINE_SECONDS
    timeout: bool = False
    timeout_reason: str | None = None

    def as_params(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "paths": self.paths,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }


@dataclass(frozen=True)
class SampledSymbol:
    """One symbol's drawn paths, or the reason there are none."""

    symbol: str
    status: str
    reason: str | None = None
    paths: tuple[dict[str, Any], ...] = ()
    duration_ms: float | None = None
    params: dict[str, Any] = field(default_factory=dict)


def _step_ms(timeframe: str) -> int:
    hours = TIMEFRAME_HOURS.get(timeframe)
    if hours is None:
        raise SamplerError(f"Kronos supports 1h/4h series, got {timeframe!r}")
    return hours * 3_600_000


def validate_settings(settings: SamplerSettings) -> str | None:
    """The reason these settings cannot produce a meaningful sample, or None.

    ``paths`` is validated at construction time, not per request: the floor is a
    sampling budget so a direction ratio is computed over enough draws to be
    reportable with its counts. It is NOT evidence of predictive skill, and the
    ratio stays a sample ratio, never a calibrated probability.
    """
    profile_for(settings.profile)
    if settings.paths < MIN_PATHS:
        return (
            f"paths={settings.paths} is below the {MIN_PATHS}-path floor; a ratio "
            "over fewer draws is a smaller sampling budget, not a stronger signal"
        )
    if settings.paths > MAX_PATHS:
        return f"paths={settings.paths} exceeds the {MAX_PATHS}-path bound"
    if settings.max_batch_rows < 1:
        return "max_batch_rows must be at least 1"
    if settings.max_batch_rows > MAX_BATCH_ROWS:
        return (
            f"max_batch_rows={settings.max_batch_rows} exceeds the {MAX_BATCH_ROWS}-row "
            "ceiling; the un-chunked 960-row configuration is the one measured at a "
            "6.07 GiB peak"
        )
    if not math.isfinite(settings.temperature) or not 0.0 < settings.temperature <= 2.0:
        return f"temperature={settings.temperature} is outside (0, 2]"
    if not math.isfinite(settings.top_p) or not 0.0 < settings.top_p <= 1.0:
        return f"top_p={settings.top_p} is outside (0, 1]"
    return None


def plan_batches(count: int, *, max_batch_rows: int, paths: int) -> list[tuple[int, int]]:
    """Split ``count`` symbols into (start, stop) chunks bounded by row budget.

    The bound is on ROWS (symbols x paths), because that is what the measured
    peak memory scales with — a batch of 60 symbols at 8 paths is half the rows
    of 60 symbols at 16 and is not the configuration that OOM'd.
    """
    if max_batch_rows < 1:
        raise SamplerError("max_batch_rows must be at least 1")
    per_symbol = max(1, int(paths))
    run = max(1, max_batch_rows // per_symbol)
    return [(start, min(start + run, count)) for start in range(0, count, run)]


class KronosSampler:
    """A warm isolated child that answers batched forecasts, bounded per call.

    Not thread-safe by design: one sampler belongs to one caller at a time, and
    the decision path calls it from the candidate thread that owns the cycle's
    budget. Concurrent use would interleave JSON lines on the same pipe.
    """

    def __init__(
        self,
        *,
        python: Any,
        source_dir: Any,
        model_dir: Any,
        settings: SamplerSettings | None = None,
        runner: Any = None,
    ) -> None:
        self._python = python
        self._source_dir = source_dir
        self._model_dir = model_dir
        # The runner path is a parameter, not a constant, so a test can drive the
        # whole pipe handshake against a stub child; production callers leave it
        # at the real runner.
        self._runner = runner if runner is not None else RUNNER_PATH
        self.settings = settings or SamplerSettings()
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self._request_counter = 0
        self._io_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    # -- process lifecycle --------------------------------------------------

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        command = [str(self._python), "-I", str(self._runner)]
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise SamplerError(f"the isolated Kronos interpreter could not start: {exc}") from exc
        # stderr MUST be drained: nothing reads this pipe, and the model's own
        # loading/progress chatter can fill the OS pipe buffer — a blocked child
        # never answers, and the parent would wait out its whole deadline. The
        # tail is kept for the error message when a request fails.
        self._stderr_tail.clear()
        assert self._process.stderr is not None
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._process.stderr,),
            name="kronos-sampler-stderr", daemon=True,
        )
        self._stderr_thread.start()
        return self._process

    def _drain_stderr(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._stderr_tail.append(line.rstrip())
                del self._stderr_tail[:-8]
        except (OSError, ValueError):  # the pipe closed with the child
            return
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def close(self) -> None:
        """Stop the child; safe to call when it was never started.

        The process reference is captured BEFORE anything else: the old code
        cleared ``self._process`` and then called ``self.kill()``, which re-read
        ``self._process`` — now None — and returned without stopping anything,
        leaving a warm model child alive after every close.
        """
        process, self._process = self._process, None
        if process is None:
            return
        self._stop(process, graceful=True)

    def kill(self) -> None:
        """Force-stop the child; same capture discipline as ``close``."""
        process, self._process = self._process, None
        if process is None:
            return
        self._stop(process, graceful=False)

    def _stop(self, process: subprocess.Popen[str], *, graceful: bool) -> None:
        """Stop the child before closing any pipe held by an I/O worker."""
        io_thread = self._io_thread
        active_io = io_thread is not None and io_thread.is_alive()
        try:
            if graceful and not active_io:
                if process.stdin is not None:
                    with contextlib.suppress(OSError, ValueError):
                        process.stdin.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=10)
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if io_thread is not None:
                io_thread.join(timeout=5)
                if io_thread.is_alive():
                    raise SamplerError("the stopped Kronos child did not release its pipe worker")
            stderr_thread = self._stderr_thread
            if stderr_thread is not None:
                stderr_thread.join(timeout=5)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                with contextlib.suppress(OSError, ValueError):
                    if stream is not None:
                        stream.close()
            self._io_thread = None
            self._stderr_thread = None

    def __enter__(self) -> KronosSampler:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- inference ----------------------------------------------------------

    def sample(
        self,
        requests: list[tuple[str, str, int, tuple[Candle, ...], int]],
    ) -> list[SampledSymbol]:
        """Forecast every ``(symbol, timeframe, horizon_hours, candles, seed)``.

        Chunked so no single child request exceeds ``max_batch_rows``. A chunk
        that overruns its deadline kills the child (the work is not interruptible
        from outside) and yields ``timeout`` for that chunk's symbols; the next
        chunk starts a fresh child. A chunk that fails for any other reason
        yields ``error`` with the child's stderr tail. No chunk is retried
        inside this call — the caller's cycle budget decides whether to retry.
        """
        if not requests:
            return []
        checked = validate_settings(self.settings)
        if checked is not None:
            raise SamplerError(checked)
        batches = plan_batches(
            len(requests), max_batch_rows=self.settings.max_batch_rows,
            paths=self.settings.paths,
        )
        results: list[SampledSymbol] = []
        with self._lock:
            for start, stop in batches:
                results.extend(self._sample_chunk(requests[start:stop]))
        return results

    def _sample_chunk(
        self, chunk: list[tuple[str, str, int, tuple[Candle, ...], int]],
    ) -> list[SampledSymbol]:
        pred_len = self._pred_len(chunk[0][1], chunk[0][2])
        payload = self._payload(chunk)
        try:
            process = self._ensure_process()
        except SamplerError as exc:
            return [self._failed(symbol, "unavailable", str(exc)) for symbol, *_ in chunk]
        try:
            answer = self._communicate(process, payload)
        except subprocess.TimeoutExpired:
            # The deadline expired with the stream state unknown: the child is
            # killed (never reused) so a late answer cannot land on the NEXT
            # request and be read as its result.
            self.kill()
            reason = (
                f"Kronos inference exceeded its {self.settings.chunk_deadline_seconds:.0f}s "
                "chunk deadline; the child was killed and the chunk is a timeout"
            )
            return [self._failed(symbol, "timeout", reason) for symbol, *_ in chunk]
        except OSError as exc:
            self.kill()
            return [
                self._failed(symbol, "unavailable", f"the child pipe failed: {exc}")
                for symbol, *_ in chunk
            ]
        if isinstance(answer, _StreamDead):
            # EOF or a line-limit overrun: the stream framing is gone. The
            # child is killed rather than kept, because the next request could
            # otherwise read an answer that belongs to this one (desync).
            self.kill()
            return [
                self._failed(symbol, "error", answer.reason)
                for symbol, *_ in chunk
            ]
        # A warm child is still RUNNING after it answers, so ``returncode`` is
        # None here — comparing it to 0 would read every successful answer as a
        # crash. Only a child that already exited with a non-zero code is one.
        if process.poll() not in (None, 0):
            self.kill()
            tail = list(self._stderr_tail) or ["no stderr"]
            return [
                self._failed(symbol, "error", "Kronos inference failed: " + " | ".join(tail))
                for symbol, *_ in chunk
            ]
        if answer.get("error"):
            # The warm child survived a bad request by answering an error line
            # (one line per request, id matched): the stream stays in step, so
            # the child is kept and the chunk gets that answer as its status.
            return [self._failed(symbol, "error", str(answer["error"])) for symbol, *_ in chunk]
        return self._results_from(chunk, answer, pred_len=pred_len)

    @staticmethod
    def _pred_len(timeframe: str, horizon_hours: int) -> int:
        hours = TIMEFRAME_HOURS.get(timeframe)
        if hours is None:
            raise SamplerError(f"Kronos supports 1h/4h series, got {timeframe!r}")
        if horizon_hours % hours:
            raise SamplerError(
                f"horizon {horizon_hours}h is not a whole number of {timeframe} bars"
            )
        pred_len = horizon_hours // hours
        if pred_len > MAX_HORIZON_BARS:
            raise SamplerError(
                f"horizon of {pred_len} bars exceeds the {MAX_HORIZON_BARS}-bar bound"
            )
        return pred_len

    def _payload(
        self, chunk: list[tuple[str, str, int, tuple[Candle, ...], int]],
    ) -> dict[str, Any]:
        request = chunk[0]
        timeframe = request[1]
        horizon_hours = request[2]
        pred_len = self._pred_len(timeframe, horizon_hours)
        # One row per (symbol, path): the model's own ``sample_count>1`` would
        # average the draws internally and destroy the dispersion the ratio and
        # band are read from, so the paths are requested as separate rows. The
        # candles are converted to plain rows HERE: the payload is JSON on a
        # pipe, and a dataclass in it would fail only inside the child.
        requests = []
        for symbol, _timeframe, _horizon, candles, _seed in chunk:
            requests.append({
                "symbol": symbol,
                "candles": [
                    [candle.open, candle.high, candle.low, candle.close, candle.volume,
                     candle.open_time]
                    for candle in candles
                ],
            })
        self._request_counter += 1
        profile = profile_for(self.settings.profile)
        return {
            # The id lets the parent prove the line it read answers the request
            # it just sent — banners and stale lines are skipped, not trusted.
            "request_id": self._request_counter,
            "requests": requests,
            "step_ms": _step_ms(timeframe),
            "pred_len": pred_len,
            "paths": self.settings.paths,
            "seeds": [seed for _s, _t, _h, _c, seed in chunk],
            "temperature": self.settings.temperature,
            "top_p": self.settings.top_p,
            "source_dir": str(self._source_dir),
            "model_dir": str(self._model_dir),
            "profile": profile.name,
            "model_repo": profile.model_repo,
            "tokenizer_repo": profile.tokenizer_repo,
            "model_revision": profile.model_revision,
            "tokenizer_revision": profile.tokenizer_revision,
            "max_context": profile.max_context,
            "model_sha256": profile.model_hashes[
                f"{profile.model_repo.rsplit('/', 1)[-1]}/model.safetensors"
            ],
            "tokenizer_sha256": profile.model_hashes[
                f"{profile.tokenizer_repo.rsplit('/', 1)[-1]}/model.safetensors"
            ],
            "model_config_sha256": profile.model_hashes[
                f"{profile.model_repo.rsplit('/', 1)[-1]}/config.json"
            ],
            "tokenizer_config_sha256": profile.model_hashes[
                f"{profile.tokenizer_repo.rsplit('/', 1)[-1]}/config.json"
            ],
        }

    # The pinned upstream model prints load banners (e.g. "Loading weights from
    # local directory") to STDOUT during from_pretrained, and a banner can even
    # look like JSON. The parent therefore skips non-JSON lines AND JSON lines
    # that do not carry THIS request's id (bounded), and returns the first line
    # that does. One line is consumed per line read, so skipping a stale line
    # resynchronises the stream instead of trusting whatever came first.
    BANNER_LINE_LIMIT = 50

    def _communicate(
        self, process: subprocess.Popen[str], payload: dict[str, Any],
    ) -> dict[str, Any] | _StreamDead:
        assert process.stdin is not None and process.stdout is not None
        deadline = time.monotonic() + self.settings.chunk_deadline_seconds
        request_id = payload["request_id"]
        stdin, stdout = process.stdin, process.stdout
        result: list[Any] = []

        def exchange() -> None:
            try:
                stdin.write(json.dumps(payload) + "\n")
                stdin.flush()
                skipped = 0
                while skipped <= self.BANNER_LINE_LIMIT:
                    line = stdout.readline()
                    if not line:
                        result.append(_StreamDead("the Kronos child closed its stdout before answering"))
                        return
                    stripped = line.strip()
                    if not stripped.startswith("{"):
                        skipped += 1
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except ValueError:
                        skipped += 1
                        continue
                    if isinstance(parsed, dict) and parsed.get("request_id") == request_id:
                        result.append(parsed)
                        return
                    skipped += 1
                result.append(_StreamDead(
                    "no answer with this request id arrived within the banner limit; "
                    "the stream framing is untrusted and the child is killed"
                ))
            except (OSError, ValueError) as exc:
                result.append(_StreamDead(f"the child pipe failed while reading or writing: {exc}"))

        worker = threading.Thread(target=exchange, name="kronos-sampler-io", daemon=True)
        self._io_thread = worker
        worker.start()
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if worker.is_alive() or time.monotonic() > deadline:
            # Killing first releases a blocked buffered write/read; closing the
            # pipe first would wait for that worker's lock without a deadline.
            self.kill()
            raise subprocess.TimeoutExpired(
                str(self._python), self.settings.chunk_deadline_seconds,
            )
        self._io_thread = None
        return result[0] if result else _StreamDead("the pipe worker produced no result")

    def _results_from(
        self,
        chunk: list[tuple[str, str, int, tuple[Candle, ...], int]],
        result: dict[str, Any],
        *,
        pred_len: int,
    ) -> list[SampledSymbol]:
        expected = profile_for(self.settings.profile)
        metadata_problem = _response_metadata_problem(result, expected)
        if metadata_problem is not None:
            return [self._failed(symbol, "error", metadata_problem) for symbol, *_ in chunk]
        raw = result.get("paths")
        duration_ms = result.get("duration_ms")
        params = {
            **self.settings.as_params(),
            **(result.get("params") or {}),
            "load_ms": result.get("load_ms"),
            "sampler_ms": result.get("sampler_ms"),
        }
        if not isinstance(raw, list) or len(raw) != len(chunk):
            reason = (
                "Kronos returned "
                + (f"{len(raw)} result(s)" if isinstance(raw, list) else "no result list")
                + f" for {len(chunk)} request(s)"
            )
            return [self._failed(symbol, "error", reason) for symbol, *_ in chunk]
        out: list[SampledSymbol] = []
        for (symbol, _t, _h, _c, _base_seed), entry in zip(chunk, raw, strict=True):
            problem = _entry_problem(entry, symbol, requested_paths=self.settings.paths,
                                     pred_len=pred_len)
            if problem is not None:
                out.append(self._failed(symbol, "error", problem))
                continue
            paths = []
            for path in entry["paths"]:
                closes = [float(value) for value in path["closes"]]
                highs = [float(value) for value in path["highs"]]
                lows = [float(value) for value in path["lows"]]
                paths.append({
                    "closes": closes, "highs": highs, "lows": lows,
                    "seed": int(path["seed"]) if path.get("seed") is not None else None,
                })
            out.append(SampledSymbol(
                symbol=symbol, status="ok", paths=tuple(paths),
                duration_ms=duration_ms if duration_ms is None else float(duration_ms),
                params=params,
            ))
        return out

    @staticmethod
    def _failed(symbol: str, status: str, reason: str) -> SampledSymbol:
        return SampledSymbol(symbol=symbol, status=status, reason=reason)


def _response_metadata_problem(result: dict[str, Any], expected: Any) -> str | None:
    """Reject a child answer that claims a different selected artifact profile."""
    fields = {
        "profile": expected.name,
        "model_revision": expected.model_revision,
        "tokenizer_revision": expected.tokenizer_revision,
        "model_repo": expected.model_repo,
        "tokenizer_repo": expected.tokenizer_repo,
        "max_context": expected.max_context,
    }
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return "Kronos response is missing profile metadata"
    for name, value in fields.items():
        if metadata.get(name) != value:
            return f"Kronos response metadata {name}={metadata.get(name)!r} does not match {value!r}"
    system = result.get("system")
    if not isinstance(system, dict):
        return "Kronos response is missing artifact hashes"
    expected_hashes = {
        "model_sha256": expected.model_hashes[
            f"{expected.model_repo.rsplit('/', 1)[-1]}/model.safetensors"
        ],
        "tokenizer_sha256": expected.model_hashes[
            f"{expected.tokenizer_repo.rsplit('/', 1)[-1]}/model.safetensors"
        ],
        "model_config_sha256": expected.model_hashes[
            f"{expected.model_repo.rsplit('/', 1)[-1]}/config.json"
        ],
        "tokenizer_config_sha256": expected.model_hashes[
            f"{expected.tokenizer_repo.rsplit('/', 1)[-1]}/config.json"
        ],
    }
    for name, value in expected_hashes.items():
        if system.get(name) != value:
            return f"Kronos response artifact hash {name}={system.get(name)!r} does not match the selected profile"
    return None


def _entry_problem(
    entry: Any, symbol: str, *, requested_paths: int, pred_len: int,
) -> str | None:
    """Why this per-symbol answer cannot be trusted, or None when it can.

    A result is accepted only when it is exactly what was requested: the symbol
    echoes back, the requested number of INDEPENDENT paths arrived (a 16-path
    request satisfied by one path is a malformed answer, not a small sample),
    every path carries the full horizon, every price is finite and positive
    with high >= low, and the per-path seeds are distinct (they are what make
    the paths independent draws). Anything else is an error, never a forecast.
    """
    if not isinstance(entry, dict):
        return "a Kronos result is not an object"
    if entry.get("symbol") != symbol:
        return f"the result names symbol {entry.get('symbol')!r}, not the requested {symbol!r}"
    paths = entry.get("paths")
    if not isinstance(paths, list):
        return "the Kronos result carries no path list"
    if len(paths) != requested_paths:
        return (
            f"the result carries {len(paths)} path(s) for a {requested_paths}-path request; "
            "a request is satisfied only by the exact number of independent draws"
        )
    for index, path in enumerate(paths):
        if not isinstance(path, dict):
            return f"path {index} is not an object"
        series = {"closes": path.get("closes"), "highs": path.get("highs"), "lows": path.get("lows")}
        for name, values in series.items():
            if not isinstance(values, list) or len(values) != pred_len:
                return (
                    f"path {index} {name} does not cover the {pred_len}-bar horizon "
                    f"(got {len(values) if isinstance(values, list) else type(values).__name__})"
                )
            for value in values:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return f"path {index} {name} carries a non-numeric value"
                if not math.isfinite(value) or value <= 0:
                    return f"path {index} {name} carries a non-finite or non-positive price {value!r}"
        highs, lows = series["highs"], series["lows"]
        if any(high < low for high, low in zip(highs, lows, strict=True)):
            return f"path {index} has a bar whose high is below its low"
        seed = path.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            return f"path {index} carries no explicit seed; per-path seeding is what makes draws independent"
    seeds = [path["seed"] for path in paths]
    if len(set(seeds)) != len(seeds):
        return "the paths' seeds are not distinct; they would not be independent draws"
    return None


@dataclass(frozen=True)
class _StreamDead:
    """The answer stream is unusable (EOF, overrun) and the child must die."""

    reason: str


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile; N is small so no library is warranted."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def passes_floor(paths: int, *, min_paths: int = MIN_PATHS) -> bool:
    """Whether a path count is enough for a ratio to carry information."""
    return int(paths) >= int(min_paths) and math.floor(paths) == paths


__all__ = [
    "DEFAULT_CHUNK_DEADLINE_SECONDS",
    "DEFAULT_MAX_BATCH_ROWS",
    "MAX_BATCH_ROWS",
    "KronosSampler",
    "SampledSymbol",
    "SamplerError",
    "SamplerSettings",
    "passes_floor",
    "percentile",
    "plan_batches",
    "validate_settings",
]
