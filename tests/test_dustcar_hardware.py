"""Hardware adapter tests: a fake ESP32-S3 firmware over real HTTP on localhost.

Nothing here talks to the robot. The fake server implements the documented
envelope, records every request, and can be told to fail a route on purpose.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from agent_system.errors import AgentError
from agent_system.hardware import (DryRunClient, DustCarCalibration, DustCarClient, DustCarExecutor,
                                   DustCarMotionCompiler, dustcar_registry, hold_chassis)
from agent_system.hardware.compiler import dustcar_registry as hardware_registry
from agent_system.models import ShotScript, UserRequest
from agent_system.mocks import MockReachabilityValidator
from agent_system.reachability import ReachabilityResult
from agent_system.storyboard import FakeStoryboardLLM, StoryboardConfig, StoryboardPlanner, to_shot_script
from webui.server import FIXTURE_STORYBOARD


class FakeFirmware(BaseHTTPRequestHandler):
    """Records requests; `fail_route` makes one path answer {"ok": false}."""

    def log_message(self, *args):
        pass

    def _handle(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append({"method": self.command, "path": parsed.path, "query": query, "body": body})
        if parsed.path == self.server.fail_route:
            return self._reply(400, {"ok": False, "error": "injected failure"})
        if parsed.path == "/api/status":
            return self._reply(200, {"mode": "idle", "battery_mv": 7400, "battery_ok": True, "speed": 0.4,
                                     "meas": [0.0] * 4, "target": [0.0] * 4, "pwm": [0] * 4,
                                     "pose_target": {"x": 0.0, "y": 0.0, "yaw": 0.0, "stored": False}})
        if parsed.path == "/api/actuator":
            return self._reply(200, {"ok": True, "ip": "127.0.0.1", "motors": [{"id": 1, "running": False, "dir": 0,
                                                                               "rpm": 0, "remain_ms": 0},
                                                                              {"id": 2, "running": False, "dir": 0,
                                                                               "rpm": 0, "remain_ms": 0}],
                                     "servos": [{"id": 1, "angle": 90}, {"id": 2, "angle": 90}]})
        self._reply(200, {"ok": True})

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle


@pytest.fixture
def firmware():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFirmware)
    server.requests = []
    server.fail_route = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.client = DustCarClient("127.0.0.1", port=server.server_address[1], timeout=2.0)
    yield server
    server.shutdown()


def paths(server):
    return [r["path"] for r in server.requests]


def test_client_endpoints_and_envelope(firmware):
    client = firmware.client
    assert client.chassis("fwd", 40) == {"ok": True}
    assert client.chassis_stop() == {"ok": True}
    assert client.motor(1, 0, 800, 3000) == {"ok": True}
    assert client.servo(2, 120) == {"ok": True}
    assert client.stop("all", True) == {"ok": True}
    assert client.status()["battery_mv"] == 7400
    assert client.actuator()["servos"] == [{"id": 1, "angle": 90}, {"id": 2, "angle": 90}]
    sent = firmware.requests
    assert sent[0] == {"method": "GET", "path": "/api/cmd", "query": {"dir": "fwd", "speed": "40"}, "body": None}
    assert sent[2]["body"] == {"id": 1, "dir": 0, "rpm": 800, "duration_ms": 3000}
    assert sent[4]["body"] == {"target": "all", "emergency": True}


def test_client_rejects_out_of_range_locally(firmware):
    for call in (lambda: firmware.client.chassis("fwd", 5), lambda: firmware.client.motor(3, 0, 800),
                 lambda: firmware.client.servo(1, 200), lambda: firmware.client.motor(1, 0, 4000),
                 lambda: firmware.client.stop("wheels")):
        with pytest.raises(AgentError) as error:
            call()
        assert error.value.code == "INVALID_PARAMETERS"
    assert firmware.requests == []  # nothing was transmitted


def test_client_reports_firmware_rejection(firmware):
    firmware.fail_route = "/api/motor"
    with pytest.raises(AgentError) as error:
        firmware.client.motor(1, 0, 800, 100)
    assert error.value.code == "DEVICE_REJECTED" and "injected failure" in error.value.reason


def test_client_reports_unreachable_device():
    dead = DustCarClient("127.0.0.1", port=1, timeout=0.5)
    with pytest.raises(AgentError) as error:
        dead.status()
    assert error.value.code == "DEVICE_UNREACHABLE"


def test_keepalive_feeds_the_watchdog(firmware):
    result = hold_chassis(firmware.client, "fwd", 0.42, speed=30, interval_ms=100)
    drives = [r for r in firmware.requests if r["query"].get("dir") == "fwd"]
    assert result["keepalives"] >= 3 and len(drives) >= 3
    assert firmware.requests[-1]["query"] == {"dir": "stop"}
    assert all(r["query"]["speed"] == "30" for r in drives)


# ---------------------------------------------------------------- compiler

def fixture_script(shot_index=0):
    board = StoryboardPlanner(FakeStoryboardLLM([{"storyboard": FIXTURE_STORYBOARD}])).plan(UserRequest(text="拍青铜器"))
    return board.shots[shot_index].target_trajectory


def compile_trajectory(trajectory, *, subject_height_mm=None, verdict="REACHABLE"):
    from agent_system.state import AgentState

    registry = hardware_registry()
    context = AgentState(plan_id="hw-test-1", shot_id="s1", registry_revision=registry.revision)
    reachability = MockReachabilityValidator(ReachabilityResult(status=verdict, reason="MOCK ONLY test verdict"))
    compiler = DustCarMotionCompiler(subject_height_mm=subject_height_mm)
    plan = compiler.compile(trajectory, context, registry, reachability)
    return compiler, plan


def test_calibration_geometry_and_unverified_constants():
    calibration = DustCarCalibration.load()
    assert round(calibration.distance_mm(0.4)) == 694 and round(calibration.distance_mm(0.7)) == 397
    assert round(calibration.pan_deg(0.333), 2) == 12.07
    assert calibration.servo_angle("pan", calibration.pan_deg(0.333)) == 78
    assert calibration.chassis_duration_ms(-297.0, 40) == 1238
    unverified = " | ".join(calibration.unverified())
    assert "mm_per_s_at_100pct" in unverified and "fy_px" in unverified


def test_compiler_turns_height_change_into_a_dolly():
    compiler, plan = compile_trajectory(fixture_script(0))
    assert plan.registry_revision == "dustcar-v1.2"
    assert [a.action_name for a in plan.actions] == ["servo_set", "chassis_drive"]
    tilt, dolly = plan.actions
    assert tilt.parameters == {"id": 2, "angle": 88}          # center_y 0.60 -> 0.55
    assert dolly.parameters == {"dir": "fwd", "speed": 40, "duration_ms": 1543}
    note = compiler.notes[0]
    assert note["dolly_mm"] == -370.4 and note["timing_ok"] is True
    assert note["distance_mm"] == [925.9, 555.6]


def test_compiler_never_emits_motion_for_a_static_shot():
    _, plan = compile_trajectory(fixture_script(1))
    assert [a.action_name for a in plan.actions] == ["actuator_stop"]


def test_compiler_refuses_oversized_single_move():
    from agent_system.models import TargetTrajectory

    trajectory = TargetTrajectory.model_validate({
        "keyframes": [{"time_offset": 0.0, "frame_state": {"center_x": 0.5, "center_y": 0.5, "subject_height_ratio": 0.2,
                                                          "distance": None}},
                      {"time_offset": 2.0, "frame_state": {"center_x": 0.5, "center_y": 0.5, "subject_height_ratio": 0.9,
                                                          "distance": None}}],
        "tolerance": {"center_x_tolerance": 0.05, "center_y_tolerance": 0.05, "height_ratio_tolerance": 0.05,
                      "distance_tolerance": None}})
    with pytest.raises(AgentError) as error:
        compile_trajectory(trajectory)
    assert error.value.code == "UNREACHABLE_MOVE" and error.value.context["dolly_mm"] == -1080.2


def test_compiler_requires_a_reachability_verdict():
    with pytest.raises(AgentError) as error:
        compile_trajectory(fixture_script(0), verdict="UNREACHABLE")
    assert error.value.code == "REACHABILITY_REFUSED"


def test_compiler_rejects_speed_outside_safety_cap():
    with pytest.raises(AgentError) as error:
        DustCarMotionCompiler(speed_pct=90)
    assert error.value.code == "INVALID_PARAMETERS"


# ---------------------------------------------------------------- executor

def hw_plan():
    _, plan = compile_trajectory(fixture_script(0))
    return plan


def test_executor_runs_the_plan_on_the_fake_firmware(firmware):
    plan = hw_plan()
    executor = DustCarExecutor(firmware.client, hardware_registry(), time_scale=0.02)
    assert executor.submit_plan(plan).status == "accepted"
    executor.start(plan.plan_id)
    executor.wait(10.0)
    assert [e.status for e in executor.events] == ["accepted", "running", "completed"]
    assert all(entry["ok"] for entry in executor.action_log)
    sent = paths(firmware)
    assert sent[0] == "/api/servo" and "/api/cmd" in sent and sent[-1] == "/api/cmd"
    servo = next(r for r in firmware.requests if r["path"] == "/api/servo")
    assert servo["body"] == {"id": 2, "angle": 88}
    assert firmware.requests[-1]["query"] == {"dir": "stop"}
    assert any(r["query"].get("dir") == "fwd" for r in firmware.requests if r["query"])


def test_executor_stops_everything_when_a_route_fails(firmware):
    firmware.fail_route = "/api/servo"
    plan = hw_plan()
    executor = DustCarExecutor(firmware.client, hardware_registry(), time_scale=0.02)
    executor.submit_plan(plan)
    executor.start(plan.plan_id)
    executor.wait(10.0)
    assert [e.status for e in executor.events] == ["accepted", "running", "failed"]
    assert "DEVICE_REJECTED" in executor.error.code
    assert {r["path"] for r in firmware.requests} >= {"/api/stop", "/api/cmd"}


def test_executor_abort_sends_a_physical_stop(firmware):
    plan = hw_plan()
    executor = DustCarExecutor(firmware.client, hardware_registry(), time_scale=0.5, keepalive_ms=40)
    executor.submit_plan(plan)
    executor.start(plan.plan_id)
    time.sleep(0.05)
    result = executor.abort()
    executor.wait(5.0)
    assert result["chassis"] == {"ok": True} and result["actuator"] == {"ok": True}
    queries = [r["query"] for r in firmware.requests if r["path"] == "/api/cmd"]
    assert {"dir": "stop"} in queries
    assert any(r["path"] == "/api/stop" for r in firmware.requests)


def test_executor_dry_run_transmits_nothing():
    plan = hw_plan()
    client = DryRunClient()
    executor = DustCarExecutor(client, hardware_registry(), time_scale=0.01)
    executor.run_blocking(plan)
    assert [entry["action_name"] for entry in executor.action_log] == ["servo_set", "chassis_drive"]
    assert client.requests[0][1] == "/api/servo"
    assert any(entry[2] == {"dir": "fwd", "speed": 40} for entry in client.requests)
    assert client.requests[-1][2] == {"dir": "stop"}


def test_executor_poll_reads_both_status_routes(firmware):
    executor = DustCarExecutor(firmware.client, hardware_registry())
    status = executor.poll()
    assert status["chassis"]["battery_mv"] == 7400 and status["actuator"]["ip"] == "127.0.0.1"


# ------------------------------------------------- feedback -> physical stop

def test_feedback_pause_triggers_a_physical_stop_on_the_device(firmware):
    """The PAUSE auto-stop must live server-side, not depend on a browser tab."""
    from agent_system.feedback import FeedbackConfig, evaluate_feedback_v2
    from agent_system.models import Observation
    from agent_system.mocks import mock_correction_capabilities
    from agent_system.state import AgentState
    import webui.server as console

    trajectory = fixture_script(0)
    plan_id, shot_id = "hw-fb-1", "s1"
    registry = hardware_registry()
    state = AgentState(plan_id=plan_id, shot_id=shot_id, registry_revision=registry.revision, status="EXECUTING")
    observation = Observation(shot_id=shot_id, plan_id=plan_id, timestamp=100.0, bbox=None)
    gate, decision, _ = evaluate_feedback_v2(trajectory, 0.0, observation, state,
                                            FeedbackConfig(max_corrections=3, max_age_seconds=1.0),
                                            mock_correction_capabilities()["ALL"],
                                            trajectory_plan_id=plan_id, now=100.0)
    assert decision.decision == "PAUSE" and decision.reason == "target_lost"

    host = f"127.0.0.1:{firmware.server_address[1]}"
    stopped = console.api_hw_stop({"host": host})
    assert stopped["result"] == {"chassis": {"ok": True}, "actuator": {"ok": True}}
    queries = [r["query"] for r in firmware.requests if r["path"] == "/api/cmd"]
    assert queries == [{"dir": "stop"}] and any(r["path"] == "/api/stop" for r in firmware.requests)


def test_console_hw_endpoints_compile_without_transmitting(firmware):
    import webui.server as console

    board = StoryboardPlanner(FakeStoryboardLLM([{"storyboard": FIXTURE_STORYBOARD}])).plan(UserRequest(text="拍青铜器"))
    script = to_shot_script(board, "dustcar-v1").model_dump()
    compiled = console.api_hw_plan({"script": script, "shot_id": script["shots"][0]["shot_id"],
                                    "host": f"127.0.0.1:{firmware.server_address[1]}",
                                    "subject_height_mm": 200, "speed_pct": 40})
    assert [a["action_name"] for a in compiled["actions"]] == ["servo_set", "chassis_drive"]
    assert compiled["commanded_seconds"] == 1.54 and compiled["within_run_limit"] is True
    assert firmware.requests == []                      # dry run transmits nothing
    assert len(compiled["unverified_constants"]) >= 15  # placeholders stay visible
    with pytest.raises(AgentError) as error:
        console.api_hw_run({"script": script, "shot_id": script["shots"][0]["shot_id"],
                            "host": f"127.0.0.1:{firmware.server_address[1]}"})
    assert error.value.code == "CONFIRMATION_REQUIRED"
