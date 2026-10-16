"""Probe the LLM relay exactly like the trading graph does.

Reuses the production client construction path (factory + tier settings) so
headers, wire protocol, timeout and retry behavior are identical to what the
autoloop sends. Run from the repo root:

    .venv\\Scripts\\python.exe -u llm_relay_check.py [probes] [model]

Exit code 0 = all probes answered; 1 = at least one failed.
"""

from __future__ import annotations

import sys
import time

import tradingagents  # noqa: F401 — loads .env before any client is built
from tradingagents.dataflows.config import get_config
from tradingagents.graph.trading_graph import _tier_llm_settings
from tradingagents.llm_clients import build_llm_kwargs, create_llm_client


def main() -> int:
    probes = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    config = get_config()
    model = sys.argv[2] if len(sys.argv) > 2 else config["quick_think_llm"]
    provider, base_url = _tier_llm_settings(config, "quick")
    client = create_llm_client(
        provider=provider, model=model, base_url=base_url,
        **build_llm_kwargs(config, provider),
    )
    llm = client.get_llm()
    print(f"probe target: provider={provider} model={model} base_url={base_url}")

    failures = 0
    for i in range(probes):
        started = time.monotonic()
        try:
            response = llm.invoke("Reply with exactly: OK")
            elapsed = time.monotonic() - started
            text = str(getattr(response, "content", response)).strip()
            print(f"probe {i + 1}: OK in {elapsed:.1f}s -> {text[:60]!r}")
        except Exception as exc:
            elapsed = time.monotonic() - started
            failures += 1
            print(f"probe {i + 1}: FAILED in {elapsed:.1f}s -> {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
