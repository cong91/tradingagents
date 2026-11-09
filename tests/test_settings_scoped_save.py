"""Offline regression tests for scoped settings save merge and uncertain PUT results."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.support.node_renderer import require_web_renderer

DRIVER = Path(__file__).with_name("test_settings_scoped_save_driver.cjs")


@pytest.fixture(scope="module")
def result():
    web_root = require_web_renderer()
    env = {
        name: os.environ[name]
        for name in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    env.update(TZ="UTC", NODE_ENV="production")
    completed = subprocess.run(
        ["node", str(DRIVER)],
        cwd=web_root,
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.unit
def test_three_way_merge_syncs_clean_values_and_preserves_concurrent_draft(result):
    scenario = result["threeWay"]
    saved = scenario["afterSave"]
    assert saved["formValues"]["clean_key"] == "clean-2"
    assert saved["formValues"]["sent_key"] == "sent-3"
    assert saved["formValues"]["concurrent_key"] == "draft-1"
    assert set(saved["dirtyKeys"]) == {"sent_key", "concurrent_key"}
    assert saved["serverAhead"] is True
    assert saved["saveState"] == {
        "kind": "saved",
        "scope": "ai",
        "appliedAt": "2026-11-09T12:01:00Z",
    }
    assert scenario["afterPoll"]["serverAhead"] is True
    assert scenario["afterPoll"]["formValues"] == saved["formValues"]
    assert scenario["afterPoll"]["conflictValues"] == {
        "concurrent_key": {"server": "remote-2", "draft": "draft-1"},
    }
    assert scenario["blockedRequestCount"] == 3
    assert [request["method"] for request in scenario["requests"]].count("PUT") == 1
    assert scenario["afterBlockedRetry"]["dirtyKeys"] == saved["dirtyKeys"]
    assert scenario["afterReview"]["formValues"]["concurrent_key"] == "draft-1"
    assert scenario["afterReview"]["serverAhead"] is False
    assert scenario["afterReview"]["conflictValues"] == {}


@pytest.mark.unit
def test_uncertain_put_reads_back_and_blocks_retry_until_review(result):
    scenario = result["uncertain"]
    uncertain = scenario["uncertain"]
    assert uncertain["saveState"] == {"kind": "uncertain", "scope": "ai"}
    assert uncertain["formValues"]["other_key"] == "draft-other"
    assert uncertain["dirtyKeys"] == ["sent_key", "other_key"]
    assert scenario["afterPoll"]["saveState"] == uncertain["saveState"]
    assert scenario["afterPoll"]["serverAhead"] is True
    assert scenario["blockedRetry"]["saveState"] == uncertain["saveState"]
    assert scenario["afterReview"]["saveState"] == {"kind": "idle"}
    assert scenario["afterReview"]["formValues"]["sent_key"] == "sent-2"
    assert scenario["afterReview"]["formValues"]["other_key"] == "draft-other"
    assert [request["method"] for request in scenario["requests"]] == ["GET", "PUT", "GET", "GET", "GET", "PUT"]
    assert scenario["afterRetry"]["saveState"]["kind"] == "saved"


@pytest.mark.unit
def test_failed_review_remains_visible_and_blocks_save_after_reset_and_poll(result):
    scenario = result["reviewFailure"]
    assert scenario["failedReview"]["saveState"]["kind"] == "uncertain"
    assert scenario["afterPollAndBlockedSave"]["saveState"]["kind"] == "uncertain"
    assert scenario["afterPollAndBlockedSave"]["formValues"]["sent_key"] == "new-draft"
    assert scenario["requests"] == ["GET", "GET", "GET", "GET"]
    assert scenario["afterSuccessfulReview"]["saveState"]["kind"] == "idle"


@pytest.mark.unit
def test_review_serializes_save_and_poll_and_rejects_older_response(result):
    scenario = result["reviewRace"]
    assert scenario["duringReview"]["reviewing"] is True
    assert scenario["requests"] == ["GET", "GET", "GET"]
    assert scenario["afterReview"]["formValues"]["clean_key"] == "clean-2"
    assert scenario["afterReview"]["formValues"]["sent_key"] == "draft"
    assert scenario["afterOldPoll"] == scenario["afterReview"]


@pytest.mark.unit
def test_discard_reads_server_before_replacing_draft_and_clearing_conflict(result):
    scenario = result["discard"]
    assert scenario["before"]["serverAhead"] is True
    assert scenario["failed"] is False
    assert scenario["afterFailure"] == {
        **scenario["before"],
        "saveState": {"kind": "uncertain", "scope": "settings"},
    }
    assert scenario["afterBlockedSave"] == scenario["afterFailure"]
    assert scenario["succeeded"] is True
    assert scenario["afterSuccess"]["saveState"] == {"kind": "idle"}
    assert scenario["afterSuccess"]["formValues"]["sent_key"] == "server-2"
    assert scenario["afterSuccess"]["dirtyKeys"] == []
    assert scenario["afterSuccess"]["serverAhead"] is False
    assert scenario["afterSuccess"]["conflictValues"] == {}
    assert scenario["requests"] == ["GET", "GET", "GET", "GET"]


@pytest.mark.unit
def test_conflict_save_bar_links_to_comparison_while_uncertain_alone_can_review(result):
    assert 'href="#settings-conflict"' in result["bars"]["conflict"]
    assert 'href="#settings-conflict"' not in result["bars"]["uncertain"]
    assert "<button" in result["bars"]["uncertain"]
