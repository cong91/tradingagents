"""Offline regression for the mini_2k child-runner/predictor boundary."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000
_BAR_COUNT = 2048
_PRED_LEN = 6


def test_mini_2k_runner_passes_2048_candles_and_six_step_context(tmp_path, monkeypatch):
    runner_path = (
        Path(__file__).resolve().parents[1]
        / "tradingagents"
        / "forecast"
        / "kronos_runner.py"
    )
    spec = importlib.util.spec_from_file_location("kronos_runner_2k_under_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    model_dir = tmp_path / "models"
    for relative in (
        "Kronos-mini/model.safetensors",
        "Kronos-mini/config.json",
        "Kronos-Tokenizer-2k/model.safetensors",
        "Kronos-Tokenizer-2k/config.json",
    ):
        artifact = model_dir / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("offline fake artifact", encoding="utf-8")

    candles = [
        [
            100.0 + index,
            101.0 + index,
            99.0 + index,
            100.5 + index,
            10.0 + index,
            1_800_000_000_000 - (1_800_000_000_000 % _FOUR_HOUR_MS) + index * _FOUR_HOUR_MS,
        ]
        for index in range(_BAR_COUNT)
    ]
    payload = {
        "request_id": "mini-2k-regression",
        "profile": "mini_2k",
        "model_repo": "NeoQuasar/Kronos-mini",
        "tokenizer_repo": "NeoQuasar/Kronos-Tokenizer-2k",
        "model_revision": "mini-model-revision",
        "tokenizer_revision": "mini-tokenizer-revision",
        "max_context": 2048,
        "source_dir": str(tmp_path / "unused-source"),
        "model_dir": str(model_dir),
        "step_ms": _FOUR_HOUR_MS,
        "pred_len": _PRED_LEN,
        "paths": 1,
        "temperature": 0.5,
        "top_p": 0.9,
        "seeds": [42],
        "requests": [{"symbol": "BTC/USDT:USDT", "candles": candles}],
    }

    predictor_configs = []
    predictions = []
    contexts_at_prediction = []
    loaded_states = []

    class FakeModule:
        def __init__(self):
            self.training = True

        def eval(self):
            self.training = False
            return self

    class FakePredictor:
        def __init__(self, model, tokenizer, *, device, max_context):
            assert model.training is False
            assert tokenizer.training is False
            loaded_states.append({
                "model_training": model.training,
                "tokenizer_training": tokenizer.training,
            })
            self.max_context = max_context
            predictor_configs.append({"device": device, "max_context": max_context})

        def predict(self, **kwargs):
            contexts_at_prediction.append(self.max_context)
            predictions.append(kwargs)
            return {
                field: [100.0] * kwargs["pred_len"]
                for field in ("open", "high", "low", "close")
            }

    model_module = types.ModuleType("model")
    model_module.Kronos = types.SimpleNamespace(
        from_pretrained=lambda _path: FakeModule(),
    )
    model_module.KronosTokenizer = types.SimpleNamespace(
        from_pretrained=lambda _path: FakeModule(),
    )
    model_module.KronosPredictor = FakePredictor

    fake_torch = types.ModuleType("torch")
    fake_torch.__version__ = "offline-test"
    fake_torch.manual_seed = lambda _seed: None
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: False,
        manual_seed_all=lambda _seed: None,
    )

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.random = types.SimpleNamespace(seed=lambda _seed: None)

    fake_pandas = types.ModuleType("pandas")
    fake_pandas.DataFrame = lambda data: data
    fake_pandas.Series = lambda values: values
    fake_pandas.to_datetime = lambda values, **_kwargs: values

    for name, module in (
        ("model", model_module),
        ("torch", fake_torch),
        ("numpy", fake_numpy),
        ("pandas", fake_pandas),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload) + "\n"))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    assert runner.main() == 0

    answer = json.loads(stdout.getvalue())
    assert predictor_configs == [{"device": "cpu", "max_context": 2048}]
    assert loaded_states == [{"model_training": False, "tokenizer_training": False}]
    assert contexts_at_prediction == [2048]
    assert len(predictions) == 1

    prediction = predictions[0]
    expected_frame = {
        name: [row[column] for row in candles]
        for name, column in zip(
            ("open", "high", "low", "close", "volume"), range(5), strict=True,
        )
    }
    assert prediction["df"] == expected_frame
    assert len(prediction["df"]["close"]) == _BAR_COUNT
    assert prediction["x_timestamp"] == [row[5] for row in candles]
    assert prediction["y_timestamp"] == [
        candles[-1][5] + _FOUR_HOUR_MS * index
        for index in range(1, _PRED_LEN + 1)
    ]
    assert prediction["pred_len"] == _PRED_LEN
    assert prediction["sample_count"] == 1
    assert answer["metadata"]["max_context"] == 2048
    assert len(answer["paths"][0]["paths"][0]["closes"]) == _PRED_LEN
