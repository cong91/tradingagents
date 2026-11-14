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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

# Pinned upstream: shiyu-coder/Kronos @ 67b630e67f6a18c9e9be918d9b4337c960db1e9a
_SOURCE_REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"


@dataclass(frozen=True)
class ArtifactProfile:
    """One closed model/tokenizer pair and its content verification pins."""

    name: str
    model_repo: str
    tokenizer_repo: str
    source_revision: str
    model_revision: str
    tokenizer_revision: str
    max_context: int
    source_hashes: Mapping[str, str]
    model_hashes: Mapping[str, str]


_SMALL_SOURCE_HASHES = MappingProxyType({
    "model/kronos.py": "0a5f90282e2039c2de0771473419715c845def154896dbd0f5747837e6241032",
    "model/module.py": "a07edbadc0e96804c8158c021bbc6063bb7cc43b34d7fc470d5c8ff2005a409f",
    "model/__init__.py": "f8f856ca3fedadcaac97e196be23d1aeda1c3c9ffe8903d66d43ea3bcac6240c",
})
_SMALL_MODEL_HASHES = MappingProxyType({
    "Kronos-Tokenizer-base/config.json":
        "2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2",
    "Kronos-Tokenizer-base/model.safetensors":
        "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee",
    "Kronos-small/config.json":
        "5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396",
    "Kronos-small/model.safetensors":
        "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020",
})
_MINI_MODEL_HASHES = MappingProxyType({
    "Kronos-Tokenizer-2k/config.json":
        "0b30a443affb03e05a876a083857de9164f899feb7b4d261da02c485c9a3e3b6",
    "Kronos-Tokenizer-2k/model.safetensors":
        "b97ec46b3b72160509e289183eaf7bdf5f0dac5bb9b49522f6d46638a99a8717",
    "Kronos-mini/config.json":
        "70daca2cb11e3a979dd6b8ac12ee08e2aace877acf28f5b8dfb4fe5609736201",
    "Kronos-mini/model.safetensors":
        "a7d5f37e2e9fbd9891f7d7d4f72574512dd1f704fee14223e0a8cd0fbf54197c",
})


PROFILES = MappingProxyType({
    "small_base": ArtifactProfile(
        name="small_base",
        model_repo="NeoQuasar/Kronos-small",
        tokenizer_repo="NeoQuasar/Kronos-Tokenizer-base",
        source_revision=_SOURCE_REVISION,
        model_revision="901c26c1332695a2a8f243eb2f37243a37bea320",
        tokenizer_revision="0e0117387f39004a9016484a186a908917e22426",
        max_context=512,
        source_hashes=_SMALL_SOURCE_HASHES,
        model_hashes=_SMALL_MODEL_HASHES,
    ),
    "mini_2k": ArtifactProfile(
        name="mini_2k",
        model_repo="NeoQuasar/Kronos-mini",
        tokenizer_repo="NeoQuasar/Kronos-Tokenizer-2k",
        source_revision=_SOURCE_REVISION,
        model_revision="f4e68697d9d5aed55cef5c96aabc3376bcad9f81",
        tokenizer_revision="26966d0035065a0cae0ebad7af8ece35bc1fb51c",
        max_context=2048,
        source_hashes=_SMALL_SOURCE_HASHES,
        model_hashes=_MINI_MODEL_HASHES,
    ),
})

DEFAULT_PROFILE = "small_base"


def profile_for(profile: str = DEFAULT_PROFILE) -> ArtifactProfile:
    """Return a selectable profile, refusing model/tokenizer hybrids."""
    try:
        return PROFILES[profile]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"unknown Kronos profile {profile!r}; supported: {sorted(PROFILES)}"
        ) from exc


# Legacy aliases: existing callers default to the verified small_base pair.
_SMALL_PROFILE = PROFILES[DEFAULT_PROFILE]
SOURCE_REVISION = _SMALL_PROFILE.source_revision
TOKENIZER_REPO = _SMALL_PROFILE.tokenizer_repo
TOKENIZER_REVISION = _SMALL_PROFILE.tokenizer_revision
MODEL_REPO = _SMALL_PROFILE.model_repo
MODEL_REVISION = _SMALL_PROFILE.model_revision
PINNED_SOURCE_HASHES = dict(_SMALL_PROFILE.source_hashes)
PINNED_WEIGHT_HASHES = dict(_SMALL_PROFILE.model_hashes)

def max_context_for(profile: str = DEFAULT_PROFILE) -> int:
    return profile_for(profile).max_context


# Bounds that keep one forecast cheap and reproducible.
MIN_LOOKBACK_BARS = 32
DEFAULT_LOOKBACK_BARS = 96
MAX_LOOKBACK_BARS = 512  # Kronos-small/base max_context
DEFAULT_PATHS = 8
MIN_PATHS = 16
MAX_PATHS = 32
MAX_HORIZON_BARS = 72

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
    configured = os.environ.get("TRADINGAGENTS_KRONOS_PYTHON")
    if configured:
        return Path(configured)
    name = "kronos-venv/Scripts/python.exe" if os.name == "nt" else "kronos-venv/bin/python"
    return _DEFAULT_RESEARCH_DIR / name


def isolated_source_dir() -> Path:
    configured = os.environ.get("TRADINGAGENTS_KRONOS_SOURCE")
    return Path(configured) if configured else _DEFAULT_RESEARCH_DIR / "kronos-src"


def isolated_model_dir() -> Path:
    configured = os.environ.get("TRADINGAGENTS_KRONOS_MODELS")
    return Path(configured) if configured else _DEFAULT_RESEARCH_DIR / "models"


# The runner moved to the neutral package with the contract it implements.
RUNNER_PATH = Path(__file__).resolve().parents[1] / "forecast" / "kronos_runner.py"


def _profile_cache_key(profile: str) -> tuple[str, str, str]:
    return (profile, "", "")


def verify_artifacts(
    source_dir: Path, model_dir: Path, profile: str = DEFAULT_PROFILE,
) -> tuple[str | None, dict[str, str]]:
    """Check the selected profile's pinned files by content.

    The result is memoized against each file's (size, mtime) signature: hashing
    the weights on every API request would be wasteful, but a swapped or
    re-downloaded file changes that signature and is re-hashed before use.
    """
    selected = profile_for(profile)
    cache_key = _profile_cache_key(selected.name)
    signature = _signature(source_dir, model_dir, selected)
    cached = _VERIFY_CACHE.get((cache_key, str(source_dir), str(model_dir)))
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    problem, verified = _verify_uncached(source_dir, model_dir, selected)
    _VERIFY_CACHE[(cache_key, str(source_dir), str(model_dir))] = (
        signature, problem, verified,
    )
    return problem, verified


_VERIFY_CACHE: dict[
    tuple[tuple[str, str, str], str, str],
    tuple[tuple[tuple[str, int, int], ...], str | None, dict[str, str]],
] = {}


def _signature(
    source_dir: Path, model_dir: Path, profile: ArtifactProfile = _SMALL_PROFILE,
) -> tuple[tuple[str, int, int], ...]:
    entries = []
    for relative in (*profile.source_hashes, *profile.model_hashes):
        path = (source_dir if relative in profile.source_hashes else model_dir) / relative
        try:
            stat = path.stat()
            entries.append((relative, stat.st_size, stat.st_mtime_ns))
        except OSError:
            entries.append((relative, -1, -1))
    return tuple(entries)


def _verify_uncached(
    source_dir: Path, model_dir: Path, profile: ArtifactProfile = _SMALL_PROFILE,
) -> tuple[str | None, dict[str, str]]:
    verified: dict[str, str] = {}
    for relative, expected in profile.source_hashes.items():
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
    for relative, expected in profile.model_hashes.items():
        path = model_dir / relative
        if not path.exists():
            return f"pinned Kronos artifact file is missing: {relative}", verified
        actual = _sha256(path)
        verified[relative] = actual
        if actual != expected:
            return (
                f"pinned Kronos artifact hash mismatch for {relative}: "
                f"{actual} != {expected}",
                verified,
            )
    return None, verified


def artifact_digest(profile: str = DEFAULT_PROFILE) -> str:
    """A stable digest of one profile's pinned source/config/weight hashes."""
    selected = profile_for(profile)
    return _digest(selected, selected.source_hashes, selected.model_hashes)


def artifact_digest_from(
    verified: dict[str, str], profile: str = DEFAULT_PROFILE,
) -> str | None:
    """Digest only the source/config/weight hashes that were actually checked."""
    selected = profile_for(profile)
    required = set(selected.source_hashes) | set(selected.model_hashes)
    if not required.issubset(verified):
        return None
    source_hashes = {name: verified[name] for name in selected.source_hashes}
    model_hashes = {name: verified[name] for name in selected.model_hashes}
    return _digest(selected, source_hashes, model_hashes)


def _digest(
    profile: ArtifactProfile,
    source_hashes: Mapping[str, str],
    model_hashes: Mapping[str, str],
) -> str:
    parts = [
        profile.name, profile.source_revision,
        f"{profile.model_repo}@{profile.model_revision}",
        f"{profile.tokenizer_repo}@{profile.tokenizer_revision}",
    ]
    parts.extend(f"{name}:{digest}" for name, digest in sorted(source_hashes.items()))
    parts.extend(f"{name}:{digest}" for name, digest in sorted(model_hashes.items()))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "ArtifactProfile",
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_PATHS",
    "DEFAULT_PROFILE",
    "MAX_HORIZON_BARS",
    "MAX_LOOKBACK_BARS",
    "MAX_PATHS",
    "MIN_LOOKBACK_BARS",
    "MIN_PATHS",
    "MODEL_REPO",
    "MODEL_REVISION",
    "PINNED_SOURCE_HASHES",
    "PINNED_WEIGHT_HASHES",
    "PROFILES",
    "RUNNER_PATH",
    "SOURCE_REVISION",
    "TOKENIZER_REPO",
    "TOKENIZER_REVISION",
    "artifact_digest",
    "artifact_digest_from",
    "isolated_model_dir",
    "isolated_python",
    "isolated_source_dir",
    "max_context_for",
    "profile_for",
    "verify_artifacts",
]
