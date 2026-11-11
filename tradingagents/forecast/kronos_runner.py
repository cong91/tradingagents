"""Child-process entry point for the pinned Kronos model (never imported in-process).

Run only by ``tradingagents.forecast.sampler`` through the operator's isolated
interpreter, outside the repo's own venv:

    <isolated python> -I tradingagents/forecast/kronos_runner.py

It imports nothing from ``tradingagents`` on purpose: the only code it executes
is the pinned upstream model source whose files the parent already hashed, so a
repo-side regression cannot change what the model does and the model cannot
change what the runtime does.

Protocol: one JSON request per line on stdin, one JSON answer per line on
stdout, in order, for the life of the process. Every answer echoes the
request's ``request_id``, so the parent can prove the line it read is the
answer to the request it just sent — the pinned model prints load banners to
stdout, and a banner that happens to look like JSON would otherwise be parsed
as an answer and leave the stream one line out of step. The model is loaded
ONCE and then stays warm — that is the entire point of the persistent child,
because the weight load costs ~2.4-2.7 s while the useful work is ~35 ms per
bar per path. A request is one batch: ``{"requests": [{"symbol", "candles"},
...], "seeds": [...]}`` with one seed per series. Every path is drawn with
``sample_count=1`` and its OWN seed, then kept separate: the caller aggregates
them. Averaging inside the model would hide the dispersion the ratio and band
are read from, and the per-path seed is what makes a forecast independent of the
batch it happened to be drawn in — the model's ``torch.multinomial`` sampling
consumes the global RNG, so without an explicit seed before each draw the result
of row k depends on how many rows preceded it.

Both flushes are load-bearing. The child's stdout is a pipe: an unflushed
answer sits in the buffer and the parent (which keeps this process warm and
never sees an exit) waits on readline until its deadline kills the request. The
parent must flush its request for the same reason.

``duration_ms`` is the WALL time of the request, measured from its first
statement: the first request of a process also carries the torch import and the
weight load, and a timer started after them under-reports the real cost by a
factor that depends on the configuration (measured: 3.4x to 43x across two
configurations). A duration that excludes the load cannot be used to decide
whether a cycle can afford the inference.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _answer(payload, predictor, device, torch, model_dir, load_ms):
    import numpy as np
    import pandas as pd

    step_ms = int(payload["step_ms"])
    pred_len = int(payload["pred_len"])
    path_count = int(payload["paths"])
    temperature = float(payload["temperature"])
    top_p = float(payload["top_p"])

    def frame_of(rows):
        frame = pd.DataFrame({
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [row[4] for row in rows],
        })
        # Timestamps are UTC-naive epoch ms: the calendar features Kronos consumes
        # (hour/weekday/month) are the venue's own clock, not the host's locale.
        x_ts = pd.Series(pd.to_datetime([row[5] for row in rows], unit="ms"))
        last_open = int(rows[-1][5])
        y_ts = pd.Series(pd.to_datetime(
            [last_open + step_ms * (index + 1) for index in range(pred_len)], unit="ms"
        ))
        return frame, x_ts, y_ts

    # One shape: a batch of series (``requests`` + one ``seeds`` entry per
    # series). Batching is how several symbols share one warm child while every
    # symbol's draw stays independent of the rows around it.
    if "requests" not in payload:
        raise ValueError(
            "the runner accepts only the batch shape: "
            '{"requests": [{"symbol", "candles"}, ...], "seeds": [...]}; '
            "build it with tradingagents.forecast.sampler.KronosSampler"
        )
    series = [
        (str(entry["symbol"]), entry["candles"], int(payload["seeds"][index]))
        for index, entry in enumerate(payload["requests"])
    ]

    # EVERY path is seeded immediately before its own draw. The model's
    # ``torch.multinomial`` consumes the process-global RNG, so without this the
    # k-th row's result depends on how many rows preceded it: the same candles
    # would give different paths depending on batch composition and order, which
    # would make any per-symbol cache and any cross-cycle comparison invalid.
    results: list[dict] = []
    started = time.perf_counter()
    for symbol, rows, base_seed in series:
        frame, x_ts, y_ts = frame_of(rows)
        paths = []
        for index in range(path_count):
            seed = base_seed + index
            torch.manual_seed(seed)
            np.random.seed(seed % (2**32 - 1))
            if device.startswith("cuda"):
                torch.cuda.manual_seed_all(seed)
            forecast = predictor.predict(
                df=frame, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=pred_len,
                T=temperature, top_p=top_p, sample_count=1, verbose=False,
            )
            paths.append({
                "seed": seed,
                "opens": [float(value) for value in forecast["open"]],
                "highs": [float(value) for value in forecast["high"]],
                "lows": [float(value) for value in forecast["low"]],
                "closes": [float(value) for value in forecast["close"]],
            })
        results.append({"symbol": symbol, "paths": paths})
    sampler_ms = (time.perf_counter() - started) * 1000.0
    # Wall time of the request as the parent experiences it: the model work plus
    # whatever share of the load this request paid (all of it for the first).
    duration_ms = sampler_ms + load_ms

    # Answers per symbol, in request order; the parent aggregates each list of
    # paths into that symbol's statistics.
    return {
        "request_id": payload.get("request_id"),
        "paths": results,
        "duration_ms": duration_ms,
        "load_ms": load_ms,
        "sampler_ms": sampler_ms,
        "torch": torch.__version__,
        "device": device,
        "python": platform.python_version(),
        "system": {
            "tokenizer_sha256": _sha256(f"{model_dir}/Kronos-Tokenizer-base/model.safetensors"),
            "model_sha256": _sha256(f"{model_dir}/Kronos-small/model.safetensors"),
        },
        "params": {
            "paths": path_count,
            "pred_len": pred_len,
            "series": len(results),
            "temperature": temperature,
            "top_p": top_p,
            "sample_count_per_path": 1,
        },
    }


def main() -> int:
    import torch

    first_request = sys.stdin.readline()
    if not first_request.strip():
        return 0
    payload = json.loads(first_request)
    source_dir = payload["source_dir"]
    model_dir = payload["model_dir"]

    process_started = time.perf_counter()
    sys.path.insert(0, source_dir)
    from model import Kronos, KronosPredictor, KronosTokenizer

    tokenizer = KronosTokenizer.from_pretrained(f"{model_dir}/Kronos-Tokenizer-base")
    model = Kronos.from_pretrained(f"{model_dir}/Kronos-small")
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
    # The load is paid once per process; the FIRST request reports it, the rest
    # report the warm ~0.3 s load the measured decomposition showed.
    load_ms = (time.perf_counter() - process_started) * 1000.0

    # The load is charged to the request that paid it: the first answer carries
    # it, later warm answers report their own work alone. Reporting the original
    # load on every request would make a warm 0.3 s lookup look like 2.8 s.
    pending_load_ms = load_ms
    # The FIRST request was read before the model load; process it first, then
    # keep serving lines until EOF. Forgetting it deadlocks the very first
    # request: the parent waits for an answer that is never produced.
    for line in _with_first(first_request, sys.stdin):
        if not line.strip():
            break
        try:
            payload = json.loads(line)
        except ValueError as exc:  # unparseable request line
            print(json.dumps({"error": f"ValueError: {exc}", "request_id": None}), flush=True)
            continue
        try:
            answer = _answer(
                payload, predictor, device, torch, model_dir,
                pending_load_ms,
            )
        except Exception as exc:  # one bad request must not kill the warm child
            answer = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            pending_load_ms = 0.0  # the load was charged to the request that paid it
        # The id echo is load-bearing even on the error path: the parent matches
        # answers to requests by it, so an error answer must carry it too.
        answer["request_id"] = payload.get("request_id") if isinstance(payload, dict) else None
        print(json.dumps(answer), flush=True)
    return 0


def _with_first(first_line: str, stream):
    """Yield the request read before the model load, then every later one."""
    yield first_line
    yield from stream


if __name__ == "__main__":
    sys.exit(main())
