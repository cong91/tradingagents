from __future__ import annotations

import pytest

from tradingagents.forecast.artifact import PROFILES, profile_for
from tradingagents.forecast.settings import ForecastSettings


def test_profile_registry_contains_only_verified_model_tokenizer_pairs():
    assert set(PROFILES) == {"small_base", "mini_2k"}

    small = profile_for("small_base")
    assert (small.model_repo, small.tokenizer_repo, small.max_context) == (
        "NeoQuasar/Kronos-small",
        "NeoQuasar/Kronos-Tokenizer-base",
        512,
    )
    assert small.model_revision == "901c26c1332695a2a8f243eb2f37243a37bea320"
    assert small.model_hashes["Kronos-small/model.safetensors"] == (
        "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"
    )

    mini = profile_for("mini_2k")
    assert (mini.model_repo, mini.tokenizer_repo, mini.max_context) == (
        "NeoQuasar/Kronos-mini",
        "NeoQuasar/Kronos-Tokenizer-2k",
        2048,
    )
    assert mini.model_revision == "f4e68697d9d5aed55cef5c96aabc3376bcad9f81"
    assert mini.tokenizer_revision == "26966d0035065a0cae0ebad7af8ece35bc1fb51c"
    assert mini.model_hashes["Kronos-mini/model.safetensors"] == (
        "a7d5f37e2e9fbd9891f7d7d4f72574512dd1f704fee14223e0a8cd0fbf54197c"
    )
    assert mini.model_hashes["Kronos-mini/config.json"] == (
        "70daca2cb11e3a979dd6b8ac12ee08e2aace877acf28f5b8dfb4fe5609736201"
    )
    assert mini.model_hashes["Kronos-Tokenizer-2k/model.safetensors"] == (
        "b97ec46b3b72160509e289183eaf7bdf5f0dac5bb9b49522f6d46638a99a8717"
    )
    assert mini.model_hashes["Kronos-Tokenizer-2k/config.json"] == (
        "0b30a443affb03e05a876a083857de9164f899feb7b4d261da02c485c9a3e3b6"
    )


def test_unknown_profile_is_not_a_selectable_artifact_pair():
    with pytest.raises(ValueError, match="unknown Kronos profile"):
        profile_for("mini_base")


def test_profile_selects_its_context_limit_and_cache_parameter():
    assert ForecastSettings(profile="small_base", lookback_bars=512).settings_problem() is None
    assert ForecastSettings(profile="small_base", lookback_bars=513).settings_problem()
    assert ForecastSettings(profile="mini_2k", lookback_bars=2048).settings_problem() is None
    assert ForecastSettings(profile="mini_2k", lookback_bars=2049).settings_problem()
    assert ForecastSettings(profile="mini_2k").as_params()["profile"] == "mini_2k"


def test_artifact_digest_is_profile_specific():
    from tradingagents.forecast.artifact import artifact_digest

    assert artifact_digest("mini_2k") != artifact_digest("small_base")
