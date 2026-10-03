"""App API tests: the phone's whole flow, service level, with a fake robot.

Every robot command goes over real HTTP to the fake firmware, so these assert what
the app would actually cause - not what the code intended.
"""

import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from agent_system.app_service import AppService
from agent_system.errors import AgentError
from agent_system.hardware import DustCarClient
from agent_system.models import UserRequest
from agent_system.storyboard import FakeStoryboardLLM, StoryboardPlanner
from tests.test_dustcar_hardware import FakeFirmware, FIXTURE_STORYBOARD
from webui.server import FIXTURE_STORYBOARD as CONSOLE_FIXTURE  # noqa: F401  (kept for parity checks)


@pytest.fixture
def robot():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFirmware)
    server.requests = []
    server.fail_route = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.requests.clear()


def planner_factory(**kwargs):
    return StoryboardPlanner(FakeStoryboardLLM([{"storyboard": FIXTURE_STORYBOARD}]),
                             config=kwargs["config"]).plan(UserRequest(text=kwargs["text"]))


def service(robot):
    return AppService(planner_factory=planner_factory,
                      client_factory=lambda host, dry_run: DustCarClient(host.partition(":")[0],
                                                                        port=int(host.partition(":")[2]), timeout=2.0),
                      robot_probe=lambda host: None)


def session(robot, **overrides):
    api = service(robot)
    created = api.create_session({"text": "拍桌面青铜器", "mode": "fixture", "host": robot,
                                  "subject_height_mm": 200, "speed_pct": 40, **overrides})
    return api, created


def frame(key, dx=0.0, dy=0.0, scale=1.0):
    half = 0.1
    return {"x1": key["center_x"] + dx - half,
            "y1": key["center_y"] + dy - key["subject_height_ratio"] * scale / 2,
            "x2": key["center_x"] + dx + half,
            "y2": key["center_y"] + dy + key["subject_height_ratio"] * scale / 2}


def test_hello_reports_calibration_and_limits(robot):
    payload = service(robot).hello({})
    assert payload["api_version"] == "app-api-1.0"
    assert payload["calibration"]["total"] == 32 and payload["calibration"]["measured"] < 32
    assert "chassis.mm_per_s_at_100pct" in payload["calibration"]["missing"]
    assert payload["capabilities"]["limits"]["max_correction_mm"] == 300.0
    assert payload["capabilities"]["registry_revision"].startswith("dustcar-")


def test_session_returns_shots_with_curves_and_tolerances(robot):
    api, created = session(robot)
    assert created["session_id"].startswith("sess-")
    assert created["total_duration"] == 7.0
    first = created["shots"][0]
    assert first["shot_id"] == "s1" and first["target_curve"][0]["center_x"] == 0.5
    assert first["tolerance"]["center_x_tolerance"] == 0.05
    assert first["camera_move"] in {"push_in", "static"}
    assert created["dry_run"] is False


def test_shot_start_needs_confirmation_for_the_real_robot(robot):
    api, created = session(robot)
    with pytest.raises(AgentError) as error:
        api.start_shot({"session_id": created["session_id"], "host": robot})
    assert error.value.code == "CONFIRMATION_REQUIRED"
    started = api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    assert [a["action_name"] for a in started["plan"]] == ["servo_set", "chassis_drive"]
    assert started["frame_contract"]["lost"].startswith("主体不在画面里")


def test_dry_run_never_touches_the_robot(robot):
    api, created = session(robot, dry_run=True)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": False})
    key = created["shots"][0]["target_curve"][0]
    row = api.observe({"session_id": created["session_id"], "frames": [frame(key, dx=0.12)]})
    assert row["results"][0]["decision"] == "ADJUST"
    assert row["results"][0]["executed"][0]["dry_run"] is True


def test_batch_observation_executes_one_correction_and_defers_the_next(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    key = created["shots"][0]["target_curve"][0]
    t0 = time.time()
    frames = [{**frame(key, dx=0.12), "timestamp": t0 - 0.30},
              {**frame(key, dy=0.08), "timestamp": t0 - 0.20},
              {**frame(key, scale=0.8), "timestamp": t0 - 0.10}]
    row = api.observe({"session_id": created["session_id"], "frames": frames})
    assert [r["decision"] for r in row["results"]] == ["ADJUST", "ADJUST", "ADJUST"]
    assert row["results"][0]["corrections"][0]["dimension"] == "PAN"
    assert row["results"][0]["executed"][0]["action_name"] == "servo_set"
    assert row["results"][1]["deferred"] and row["results"][1]["executed"] is None
    assert any("推迟" in hint for hint in row["hints"])
    assert row["summary"]["frames"] == 3


def test_lost_subject_pauses_and_stops_the_chassis(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    row = api.observe({"session_id": created["session_id"], "lost": True})
    assert row["results"][0]["decision"] == "PAUSE" and row["results"][0]["reason"] == "target_lost"
    assert row["results"][0]["executed"]["chassis"] == {"ok": True}


def test_future_timestamp_is_refused_after_the_clock_is_aligned(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    key = created["shots"][0]["target_curve"][0]
    # First batch aligns the phone's time base; only then can "in the future" be detected.
    api.observe({"session_id": created["session_id"],
                 "frames": [{**frame(key), "timestamp": time.time()}]})
    row = api.observe({"session_id": created["session_id"],
                       "frames": [{**frame(key), "timestamp": time.time() + 30}]})
    assert row["results"][0]["gate"]["code"] == "INVALID_OBSERVATION"
    assert any("未来" in hint for hint in row["hints"])


def test_explicit_clock_offset_is_honoured(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    key = created["shots"][0]["target_curve"][0]
    phone_clock = 500.0                                     # phone counts seconds since boot
    row = api.observe({"session_id": created["session_id"], "clock_offset_s": time.time() - phone_clock,
                       "frames": [{**frame(key), "timestamp": phone_clock}, {**frame(key), "timestamp": phone_clock - 0.2}]})
    assert [r["decision"] for r in row["results"]] == ["CONTINUE", "CONTINUE"]
    assert abs(row["summary"]["clock_offset"] - (time.time() - phone_clock)) < 1.0


def test_clock_offset_aligns_on_the_newest_frame(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    key = created["shots"][0]["target_curve"][0]
    t0 = time.time() - 100.0                      # phone clock far behind the agent's
    api.observe({"session_id": created["session_id"],
                 "frames": [{**frame(key), "timestamp": t0 - 0.2}, {**frame(key), "timestamp": t0}]})
    summary = api.session_state({"session_id": created["session_id"]})["summary"]
    assert abs(summary["clock_offset"] - 100.0) < 1.0
    assert summary["decisions"] == ["CONTINUE", "CONTINUE"]


def test_out_of_range_box_is_rejected_without_moving(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    with pytest.raises(AgentError) as error:
        api.observe({"session_id": created["session_id"], "x1": 0.2, "y1": 0.2, "x2": 1.4, "y2": 0.8})
    assert error.value.code == "INVALID_OBSERVATION"


def test_advance_moves_to_the_next_shot_and_finishes(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    moved = api.advance({"session_id": created["session_id"], "host": robot, "confirm": True})
    assert moved["shot"]["shot_id"] == created["shots"][1]["shot_id"]
    assert moved["stopped_previous"]["chassis"] == {"ok": True}
    done = api.advance({"session_id": created["session_id"]})
    assert done["finished"] is True


def test_pixel_batch_inherits_the_top_level_frame_size(robot):
    api, created = session(robot)
    api.start_shot({"session_id": created["session_id"], "host": robot, "confirm": True})
    key = created["shots"][0]["target_curve"][0]
    norm = frame(key, dx=0.12)
    pixels = {name: round(norm[name] * (1920 if name in ("x1", "x2") else 1080)) for name in norm}
    row = api.observe({"session_id": created["session_id"], "frame_width": 1920, "frame_height": 1080,
                       "frames": [{"timestamp": time.time(), **pixels}]})
    assert abs(row["results"][0]["box"]["x1"] - (pixels["x1"] / 1920)) < 1e-6
    assert row["results"][0]["decision"] == "ADJUST"


def test_unknown_session_is_a_clear_error(robot):
    api = service(robot)
    with pytest.raises(AgentError) as error:
        api.observe({"session_id": "sess-nope", "lost": True})
    assert error.value.code == "NO_SESSION"
    with pytest.raises(AgentError) as not_started:
        api.observe({"session_id": api.create_session({"text": "拍一下", "mode": "fixture",
                                                       "host": robot})["session_id"], "lost": True})
    assert not_started.value.code == "SHOT_NOT_STARTED"


# ------------------------------------------------------- 连接预算（App 只给 1.5s 读、3s 总）

class CountingProbe:
    """Stand-in for the robot probe: counts calls, can be slow or failing."""

    def __init__(self, delay=0.0, error=None):
        self.delay, self.error, self.calls = delay, error, []

    def __call__(self, host):
        self.calls.append(host)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return {"mode": "idle", "battery_mv": 7400}


def probe_service(probe, wait_s=0.9):
    return AppService(planner_factory=planner_factory,
                      client_factory=lambda host, dry_run: None,
                      robot_probe=probe, probe_wait_s=wait_s)


def test_hello_never_waits_on_an_unreachable_robot():
    """App 读超时 1.5s：hello 最多等 probe_wait_s，剩下的在后台继续。"""
    probe = CountingProbe(delay=5.0)
    service_ = probe_service(probe, wait_s=0.4)
    started = time.perf_counter()
    payload = service_.hello({"host": "192.168.4.1", "probe": True})
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5, f"hello 等了 {elapsed:.2f}s，会被 App 判 TIMEOUT"
    assert payload["ok"] is True and payload["robot"]["reachable"] is None
    assert payload["robot"]["state"] == "checking"
    assert payload["robot"]["hint"]


def test_hello_reports_a_dead_robot_and_caches_the_verdict():
    probe = CountingProbe(error=AgentError("DEVICE_UNREACHABLE", "机器人无响应"))
    service_ = probe_service(probe)
    first = service_.hello({"host": "192.168.4.1", "probe": True})
    assert first["robot"]["reachable"] is False
    assert first["robot"]["state"] == "unreachable"
    assert first["robot"]["error"]["code"] == "DEVICE_UNREACHABLE"
    second = service_.hello({"host": "192.168.4.1", "probe": True})
    assert second["robot"]["state"] == "unreachable"
    assert probe.calls == ["192.168.4.1"], "第二次 hello 应读缓存，不再探测"


def test_hello_reports_a_reachable_robot_with_its_status():
    probe = CountingProbe()
    payload = probe_service(probe).hello({"host": "192.168.4.1", "probe": True})
    assert payload["robot"]["reachable"] is True
    assert payload["robot"]["state"] == "ready"
    assert payload["robot"]["status"]["battery_mv"] == 7400


def test_hello_probes_again_when_the_robot_host_changes():
    probe = CountingProbe()
    service_ = probe_service(probe)
    service_.hello({"host": "192.168.4.1", "probe": True})
    service_.hello({"host": "10.0.0.9", "probe": True})
    assert probe.calls == ["192.168.4.1", "10.0.0.9"]


def test_hello_without_probe_returns_at_once():
    probe = CountingProbe(delay=5.0)
    payload = probe_service(probe).hello({"host": "192.168.4.1"})
    assert payload["robot"]["state"] == "not_requested"
    assert payload["robot"]["reachable"] is None
    assert probe.calls == []


def test_a_dead_robot_is_not_re_probed_within_the_failure_window():
    """机器人离线时握手不该每次都等探测超时。"""
    from agent_system.app_service import RobotProbe
    probe = CountingProbe(error=AgentError("DEVICE_UNREACHABLE", "机器人无响应"))
    cache = RobotProbe(probe, wait_s=0.0, fail_cache_s=0.3)
    cache.snapshot("192.168.4.1")
    cache.snapshot("192.168.4.1")
    assert probe.calls == ["192.168.4.1"], "失败窗口内应读缓存"
    time.sleep(0.35)
    cache.snapshot("192.168.4.1")
    assert len(probe.calls) == 2, "过了失败窗口才重新探测"


def test_ping_is_a_cheap_path_with_no_robot_probe():
    probe = CountingProbe(delay=5.0)
    payload = probe_service(probe).ping({})
    assert payload["ok"] is True and payload["api_version"] == "app-api-1.0"
    assert payload["robot_state"] == "unknown"
    assert probe.calls == []


def test_probe_wait_can_be_overridden_per_request():
    probe = CountingProbe(delay=0.6)
    started = time.perf_counter()
    payload = probe_service(probe).hello({"host": "192.168.4.1", "probe": True, "probe_wait_s": 1.2})
    assert time.perf_counter() - started >= 0.6
    assert payload["robot"]["state"] == "ready"


def test_the_requirement_is_journalled_and_summarised():
    """手机发出的需求必须可回看（内存会话会随重启消失）。"""
    seen = []
    service_ = AppService(planner_factory=planner_factory,
                          client_factory=lambda host, dry_run: None,
                          robot_probe=lambda host: None, journal=seen.append)
    created = service_.create_session({"text": "拍桌面青铜器，从偏低占 40% 升到中部放大 70%"})
    assert seen and seen[0]["text"] == "拍桌面青铜器，从偏低占 40% 升到中部放大 70%"
    assert seen[0]["kind"] == "create_session" and seen[0]["session_id"] == created["session_id"]
    rows = service_.summaries()
    assert len(rows) == 1 and rows[0]["text"].startswith("拍桌面青铜器")
    assert rows[0]["shots"] >= 1 and rows[0]["shot_started"] is False


def test_console_records_app_requests_for_this_side_diagnosis():
    import webui.server as console
    entry_before = len(console.APP_LOG)
    console._note_app_request(_FakeHandler(), "/api/app/hello", 200, 12.0)
    payload = console.api_applog({})
    assert payload["ok"] is True and len(console.APP_LOG) == entry_before + 1
    assert payload["entries"][-1]["path"] == "/api/app/hello"


class _FakeHandler:
    command = "POST"
    client_address = ("192.168.4.44", 5555)
    headers = {"User-Agent": "AgentApp/0.6.0 (Android)"}
