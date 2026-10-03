"""Offline Agent 1 storyboard tests; scripted LLM, no network."""

from copy import deepcopy

import pytest

from agent_system.errors import AgentError
from agent_system.models import UserRequest
from agent_system.storyboard import (FakeStoryboardLLM, StoryboardConfig, StoryboardPlanner,
                                     consistency_warnings, timeline, to_shot_script)
from webui.server import FIXTURE_STORYBOARD


def _plan(*replies, config=None):
    llm = FakeStoryboardLLM([{"storyboard": deepcopy(r)} if r is not None and "shots" in r else r for r in replies])
    return llm, StoryboardPlanner(llm, config=config).plan(UserRequest(text="拍青铜器"))


def test_valid_storyboard_and_derived_timeline():
    _, board = _plan(FIXTURE_STORYBOARD)
    rows = timeline(board)
    assert [(r["start"], r["end"]) for r in rows] == [(0.0, 4.0), (4.0, 7.0)]
    assert rows[0]["camera_move_zh"] == "推" and rows[1]["shot_size_zh"] == "特写"
    assert consistency_warnings(board) == []


def test_bridge_to_shot_script_keeps_trajectory():
    _, board = _plan(FIXTURE_STORYBOARD)
    script = to_shot_script(board, "mock-v2-demo")
    assert script.schema_version == "0.2" and len(script.shots) == 2
    assert script.shots[0].target_trajectory == board.shots[0].target_trajectory
    assert script.shots[0].shot_goal.startswith("[远景/仰拍/推]")


def test_schema_error_is_repaired_once():
    broken = deepcopy(FIXTURE_STORYBOARD)
    broken["shots"][0]["tolerance"] = broken["shots"][0]["target_trajectory"].pop("tolerance")
    llm, board = _plan(broken, FIXTURE_STORYBOARD)
    assert len(llm.calls) == 2 and llm.calls[1]["repair_error"]["code"] == "SCHEMA_ERROR"
    assert len(board.shots) == 2


def test_duration_mismatch_is_repaired_then_fails():
    broken = deepcopy(FIXTURE_STORYBOARD)
    broken["shots"][0]["duration"] = 5.0
    with pytest.raises(AgentError) as error:
        _plan(broken, broken)
    assert error.value.code == "INVALID_PLAN"


def test_null_storyboard_is_refusal_without_retry():
    llm = FakeStoryboardLLM([{"storyboard": None}])
    with pytest.raises(AgentError) as error:
        StoryboardPlanner(llm).plan(UserRequest(text="输出PWM"))
    assert error.value.code == "CAPABILITY_VIOLATION" and len(llm.calls) == 1


def test_disallowed_move_rejected():
    config = StoryboardConfig(allowed_moves=["static"])
    with pytest.raises(AgentError):
        _plan(FIXTURE_STORYBOARD, FIXTURE_STORYBOARD, config=config)


def test_total_duration_bound():
    with pytest.raises(AgentError) as error:
        _plan(FIXTURE_STORYBOARD, FIXTURE_STORYBOARD, config=StoryboardConfig(max_total_duration=5.0))
    assert error.value.code == "INVALID_PLAN"


def test_push_in_with_shrinking_subject_warns():
    flipped = deepcopy(FIXTURE_STORYBOARD)
    kf = flipped["shots"][0]["target_trajectory"]["keyframes"]
    kf[0]["frame_state"]["subject_height_ratio"], kf[1]["frame_state"]["subject_height_ratio"] = 0.5, 0.3
    _, board = _plan(flipped)
    assert [w["shot_id"] for w in consistency_warnings(board)] == ["s1"]
