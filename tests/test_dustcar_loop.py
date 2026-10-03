"""Closed-loop tests: green box -> Gate -> Feedback V2 -> real HTTP commands.

Still no robot: the fake firmware on localhost records every request, so the
assertions are about what the agent actually sent, not about intentions.
"""

import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_system.errors import AgentError
from agent_system.feedback import evaluate_feedback_v2
from agent_system.hardware import DustCarCalibration, DustCarClient, VisionLoop, dustcar_registry
from agent_system.hardware.corrections import CorrectionCompiler
from agent_system.mocks import MockReachabilityValidator
from agent_system.models import Observation, TrajectoryCorrectionIntent, UserRequest
from agent_system.reachability import ReachabilityResult
from agent_system.storyboard import FakeStoryboardLLM, StoryboardPlanner
from tests.test_dustcar_hardware import FakeFirmware, FIXTURE_STORYBOARD


@pytest.fixture
def firmware():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFirmware)
    server.requests = []
    server.fail_route = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.client = DustCarClient("127.0.0.1", port=server.server_address[1], timeout=2.0)
    yield server
    server.shutdown()


def shot(index=0):
    board = StoryboardPlanner(FakeStoryboardLLM([{"storyboard": FIXTURE_STORYBOARD}])).plan(UserRequest(text="拍青铜器"))
    return board.shots[index]


def make_loop(firmware, **kwargs):
    loop = VisionLoop(firmware.client, shot=shot(), registry=dustcar_registry(),
                      calibration=DustCarCalibration.load(), subject_height_mm=200, speed_pct=40, **kwargs)
    loop.start(MockReachabilityValidator(ReachabilityResult(status="REACHABLE", reason="test")))
    return loop


def box(cx, cy, h):
    return {"x1": cx - 0.1, "y1": cy - h / 2, "x2": cx + 0.1, "y2": cy + h / 2}


def paths(firmware):
    return [r["path"] for r in firmware.requests]


def test_correction_maps_a_lateral_error_to_a_servo_move(firmware):
    loop = make_loop(firmware)
    row = loop.observe(**box(0.62, 0.60, 0.30))          # 0.12 to the right of the 0.5 target
    assert row["decision"]["decision"] == "ADJUST"
    assert row["decision"]["correction_intent"]["components"][0]["dimension"] == "CENTER_X"
    assert row["corrections"][0]["dimension"] == "PAN"
    servo = next(r for r in firmware.requests if r["path"] == "/api/servo")
    assert servo["body"] == {"id": 1, "angle": 81}       # +8.74 deg, sign -1, zero 90
    assert "/api/cmd" not in paths(firmware)             # no dolly for a lateral error


def test_correction_maps_a_size_error_to_a_dolly(firmware):
    loop = make_loop(firmware)
    row = loop.observe(**box(0.5, 0.60, 0.24))           # subject smaller than the 0.30 target
    assert row["decision"]["correction_intent"]["components"][0]["dimension"] == "SUBJECT_HEIGHT_RATIO"
    assert -232 < row["corrections"][0]["travel_mm"] < -231     # D(0.30)-D(0.24) = -231.5mm
    drive = [r for r in firmware.requests if r["path"] == "/api/cmd" and r["query"].get("dir") != "stop"]
    assert drive and drive[0]["query"] == {"dir": "fwd", "speed": "40"}


def test_correction_is_clamped_and_reported(firmware):
    loop = make_loop(firmware)
    # A narrow box near the right edge stays inside the frame while asking for a 32 deg turn.
    row = loop.observe(x1=0.981, y1=0.45, x2=0.999, y2=0.75)
    assert row["corrections"][0]["clamped"] is True
    assert abs(row["corrections"][0]["delta_deg"]) == loop.budget.max_correction_deg


def test_corrections_respect_the_minimum_interval(firmware):
    loop = make_loop(firmware)
    first = loop.observe(**box(0.62, 0.60, 0.30))
    before = len(firmware.requests)
    second = loop.observe(**box(0.62, 0.60, 0.30))
    assert first["executed"] and second["deferred"] and len(firmware.requests) == before


def test_correction_budget_ends_in_pause_and_stop(firmware):
    loop = make_loop(firmware, max_corrections=1)
    loop.observe(**box(0.62, 0.60, 0.30))
    loop.observe(**box(0.66, 0.60, 0.30))
    final = loop.observe(**box(0.70, 0.60, 0.30))
    assert final["decision"]["decision"] == "PAUSE"
    assert final["decision"]["reason"] == "correction_budget_exhausted"
    assert "/api/stop" in paths(firmware)


def test_correction_compiler_refuses_a_move_below_resolution():
    intent = TrajectoryCorrectionIntent(shot_id="s1", plan_id="p1", trajectory_time=0.0, reason="x",
                                        components=[{"dimension": "DISTANCE", "target_value": 2.0,
                                                     "observed_value": 2.5, "error": 0.5}])
    compiler = CorrectionCompiler(DustCarCalibration.load(), subject_height_mm=200, speed_pct=40)
    with pytest.raises(AgentError) as error:             # 0.6mm at 240mm/s = 2ms, under min_action_ms
        compiler.compile(intent, dustcar_registry())
    assert error.value.code == "UNSUPPORTED_CORRECTION"


def test_loop_moves_only_when_the_box_is_off_target(firmware):
    loop = make_loop(firmware)
    ok = loop.observe(**box(0.5, 0.60, 0.30))            # exactly on target at t=0
    assert ok["decision"]["decision"] == "CONTINUE"
    assert firmware.requests == []
    off = loop.observe(**box(0.5, 0.60, 0.24))
    assert off["decision"]["decision"] == "ADJUST" and firmware.requests


def test_loop_stops_the_rig_when_the_target_is_lost(firmware):
    loop = make_loop(firmware)
    now = loop.clock()
    gate, decision, state = evaluate_feedback_v2(
        loop.trajectory, 0.0, Observation(shot_id=loop.shot.shot_id, plan_id=loop.plan_id, timestamp=now, bbox=None),
        loop.state, loop.config, loop.capability, trajectory_plan_id=loop.plan_id, now=now)
    assert decision.decision == "PAUSE" and decision.reason == "target_lost"
    stopped = loop._stop(decision.reason)
    assert stopped["chassis"] == {"ok": True} and stopped["actuator"] == {"ok": True}
    assert {"dir": "stop"} in [r["query"] for r in firmware.requests if r["path"] == "/api/cmd"]


def test_loop_rejects_boxes_the_contract_cannot_use(firmware):
    loop = make_loop(firmware)
    stale = loop.observe(**box(0.62, 0.60, 0.30), timestamp=loop.clock() - 5.0)   # older than max_age 1.0s
    assert stale["gate"]["accepted"] is False and stale["gate"]["code"] == "STALE"
    with pytest.raises(AgentError) as outside:
        loop.observe(x1=0.2, y1=0.2, x2=1.4, y2=0.8)
    assert outside.value.code == "INVALID_OBSERVATION"
    assert firmware.requests == []


def test_loop_accepts_pixel_coordinates_from_the_vision_module(firmware):
    loop = make_loop(firmware)
    row = loop.observe(x1=0.62 * 1920 - 192, y1=0.6 * 1080 - 162, x2=0.62 * 1920 + 192, y2=0.6 * 1080 + 162,
                       frame_width=1920, frame_height=1080)
    assert abs(row["box"]["x1"] - 0.52) < 1e-6 and abs(row["box"]["x2"] - 0.72) < 1e-6
    assert row["decision"]["decision"] == "ADJUST"
