"""Pinned Kronos artifacts: the content hashes a forecast is allowed to run under.

The model is pinned by content, not by name. Every run re-checks the SHA-256 of
the three pinned source files and of the weight files before the child process
imports them; a swapped or partially downloaded artifact fails closed instead of
quietly forecasting from something else. Revisions and hashes are recorded on
every result, so no row can claim a model revision it did not run.

Nothing heavy is a dependency of this module: ``torch``, ``pandas`` and the
Kronos source are never imported here. The provider shells out to an interpreter
the operator installed outside the repo, so the runtime venv and the shipped
dependencies are untouched and the module imports cleanly when the model is
absent.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Pinned upstream: shiyu-coder/Kronos @ 67b630e67f6a18c9e9be918d9b4337c960db1e9a
SOURCE_REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
TOKENIZER_REPO = "NeoQuasar/Kronos-Tokenizer-base"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
MODEL_REPO = "NeoQuasar/Kronos-small"
MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"

# Content pins: the source files this adapter will import and the weights it will
# load. A re-download that changes even one byte is refused rather than trusted.
PINNED_SOURCE_HASHES = {
    "model/kronos.py": "0a5f90282e2039c2de0771473419715c845def154896dbd0f5747837e6241032",
    "model/module.py": "a07edbadc0e96804c8158c021bbc6063bb7cc43b34d7fc470d5c8ff2005a409f",
    "model/__init__.py": "f8f856ca3fedadcaac97e196be23d1aeda1c3c9ffe8903d66d43ea3bcac6240c",
}
PINNED_WEIGHT_HASHES = {
    "Kronos-Tokenizer-base/model.safetensors":
        "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee",
    "Kronos-small/model.safetensors":
        "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020",
}

# Bounds that keep one forecast cheap and reproducible.
MIN_LOOKBACK_BARS = 32
DEFAULT_LOOKBACK_BARS = 96
MAX_LOOKBACK_BARS = 512  # Kronos-small/base max_context
DEFAULT_PATHS = 8
MIN_PATHS = 16
MAX_PATHS = 32
MAX_HORIZON_BARS = 72

TIMEFRAME_HOURS = {"1h": 1, "4h": 4}

_HOME = Path(os.path.expanduser("~"))
_DEFAULT_RESEARCH_DIR = Path(
    os.environ.get("LOCALAPPDATA") or _HOME
) / "tradingagents-research"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def isolated_python() -> Path:
    """The operator's isolated interpreter; outside the repo by default."""
    configured = os.environ.get("TRADINGAGENTS_SHADOW_KRONOS_PYTHON")
    if configured:
        return Path(configured)
    name = "kronos-venv/Scripts/python.exe" if os.name == "nt" else "kronos-venv/bin/python"
    return _DEFAULT_RESEARCH_DIR / name


def isolated_source_dir() -> Path:
    configured = os.environ.get("TRADINGAGENTS_SHADOW_KRONOS_SOURCE")
    return Path(configured) if configured else _DEFAULT_RESEARCH_DIR / "kronos-src"


def isolated_model_dir() -> Path:
    configured = os.environ.get("TRADINGAGENTS_SHADOW_KRONOS_MODELS")
    return Path(configured) if configured else _DEFAULT_RESEARCH_DIR / "models"


# The runner moved to the neutral package with the contract it implements.
RUNNER_PATH = Path(__file__).resolve().parents[1] / "forecast" / "kronos_runner.py"


def verify_artifacts(source_dir: Path, model_dir: Path) -> tuple[str | None, dict[str, str]]:
    """Check the pinned files by content; returns (problem, verified hashes).

    The result is memoized against each file's (size, mtime) signature: hashing
    ~115 MB of weights on every API request would be wasteful, but a swapped or
    re-downloaded file changes that signature and is re-hashed before use.
    """
    signature = _signature(source_dir, model_dir)
    cached = _VERIFY_CACHE.get((str(source_dir), str(model_dir)))
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    problem, verified = _verify_uncached(source_dir, model_dir)
    _VERIFY_CACHE[(str(source_dir), str(model_dir))] = (signature, problem, verified)
    return problem, verified


_VERIFY_CACHE: dict[
    tuple[str, str], tuple[tuple[tuple[str, int, int], ...], str | None, dict[str, str]]
] = {}


def _signature(source_dir: Path, model_dir: Path) -> tuple[tuple[str, int, int], ...]:
    entries = []
    for relative in (*PINNED_SOURCE_HASHES, *PINNED_WEIGHT_HASHES):
        root = source_dir if relative in PINNED_SOURCE_HASHES else model_dir
        path = root / relative
        try:
            stat = path.stat()
            entries.append((relative, stat.st_size, stat.st_mtime_ns))
        except OSError:
            entries.append((relative, -1, -1))
    return tuple(entries)


def _verify_uncached(source_dir: Path, model_dir: Path) -> tuple[str | None, dict[str, str]]:
    verified: dict[str, str] = {}
    for relative, expected in PINNED_SOURCE_HASHES.items():
        path = source_dir / relative
        if not path.exists():
            return f"pinned Kronos source file is missing: {relative}", verified
        actual = _sha256(path)
        verified[relative] = actual
        if actual != expected:
            return (
                f"pinned Kronos source hash mismatch for {relative}: "
                f"{actual} != {expected}",
                verified,
            )
    for relative, expected in PINNED_WEIGHT_HASHES.items():
        path = model_dir / relative
        if not path.exists():
            return f"pinned Kronos weight file is missing: {relative}", verified
        actual = _sha256(path)
        verified[relative] = actual
        if actual != expected:
            return (
                f"pinned Kronos weight hash mismatch for {relative}: "
                f"{actual} != {expected}",
                verified,
            )
    return None, verified


def artifact_digest() -> str:
    """A stable digest of the pinned source/weight hashes this adapter expects."""
    parts = [SOURCE_REVISION, MODEL_REVISION, TOKENIZER_REVISION]
    parts.extend(f"{name}:{digest}" for name, digest in sorted(PINNED_SOURCE_HASHES.items()))
    parts.extend(f"{name}:{digest}" for name, digest in sorted(PINNED_WEIGHT_HASHES.items()))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def artifact_digest_from(verified: dict[str, str]) -> str | None:
    """The digest of the hashes that were ACTUALLY checked, or None.

    ``artifact_digest()`` describes the pins a run is ALLOWED under; this
    describes what a run verified on disk. A result may only carry this digest
    when ``verify_artifacts`` succeeded — an expected hash must never be
    labelled as one that was checked.
    """
    required = set(PINNED_SOURCE_HASHES) | set(PINNED_WEIGHT_HASHES)
    if not required.issubset(verified):
        return None
    parts = [SOURCE_REVISION, MODEL_REVISION, TOKENIZER_REVISION]
    parts.extend(f"{name}:{verified[name]}" for name in sorted(PINNED_SOURCE_HASHES))
    parts.extend(f"{name}:{verified[name]}" for name in sorted(PINNED_WEIGHT_HASHES))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_PATHS",
    "MAX_HORIZON_BARS",
    "MAX_LOOKBACK_BARS",
    "MAX_PATHS",
    "MIN_LOOKBACK_BARS",
    "MIN_PATHS",
    "MODEL_REPO",
    "MODEL_REVISION",
    "PINNED_SOURCE_HASHES",
    "PINNED_WEIGHT_HASHES",
    "RUNNER_PATH",
    "SOURCE_REVISION",
    "TIMEFRAME_HOURS",
    "TOKENIZER_REPO",
    "TOKENIZER_REVISION",
    "artifact_digest",
    "artifact_digest_from",
    "isolated_model_dir",
    "isolated_python",
    "isolated_source_dir",
    "verify_artifacts",
]
