"""ShotPilot 0.6.0 client payloads against the live AppService.

The Android app is the entry point: pixel boxes, fixture+dry_run by default,
confirm:true even when rehearsing, and lost frames as {lost:true}. These tests
lock that wire shape so a hello/observe change cannot silently break the phone.
"""

import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from tests.test_app_api import frame, service, session
from tests.test_dustcar_hardware import FakeFirmware


@pytest.fixture
def robot():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFirmware)
    server.requests = []
    server.fail_route = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.requests.clear()


def shotpilot_session_body(hello, robot, text="拍桌面青铜器"):
    limits = hello["capabilities"]["limits"]
    return {
        "text": text,
        "host": robot,
        "speed_pct": 20,
        "max_shots": limits["max_shots"],
        "max_shot_duration": limits["max_shot_duration_s"],
        "max_total_duration": limits["max_total_duration_s"],
        "allowed_moves": hello["capabilities"]["moves"],
        "mode": "fixture",
        "dry_run": True,
        "max_age_seconds": 1.0,
    }


def test_shotpilot_hello_allows_null_reachable_when_probe_is_unsupported():
    api = service("127.0.0.1:9")
    api.probe = None
    payload = api.hello({"host": "192.168.4.1", "probe": True, "probe_wait_s": 1.2})
    assert payload["api_version"] == "app-api-1.0"
    assert payload["robot"]["state"] == "unsupported"
    assert payload["robot"]["reachable"] is None


def test_shotpilot_pixel_observe_and_lost_object_receipts(robot):
    api = service(robot)
    hello = api.hello({"host": robot, "probe": True, "probe_wait_s": 1.2})
    created = api.create_session(shotpilot_session_body(hello, robot))
    assert created["dry_run"] is True
    shot = created["shots"][0]
    started = api.start_shot({
        "session_id": created["session_id"],
        "shot_id": shot["shot_id"],
        "host": robot,
        "speed_pct": 20,
        "confirm": True,
        "reachability": "REACHABLE",
    })
    assert started["dry_run"] is True
    key = shot["target_curve"][0]
    norm = frame(key, dx=0.12)
    pixels = {
        "x1": norm["x1"] * 1280,
        "y1": norm["y1"] * 720,
        "x2": norm["x2"] * 1280,
        "y2": norm["y2"] * 720,
    }
    observed = api.observe({
        "session_id": created["session_id"],
        **pixels,
        "frame_width": 1280,
        "frame_height": 720,
        "timestamp": time.time(),
        "space": "pixel",
    })
    assert observed["session_id"] == created["session_id"]
    assert observed["plan_id"] == started["plan_id"]
    assert len(observed["results"]) == 1
    row = observed["results"][0]
    assert row["decision"] in {"CONTINUE", "ADJUST"}
    if row["decision"] == "ADJUST":
        assert isinstance(row["executed"], list)
        assert row["executed"][0]["dry_run"] is True
        assert "ok" not in row["executed"][0]
    lost = api.observe({"session_id": created["session_id"], "lost": True})
    pause = lost["results"][0]
    assert pause["decision"] == "PAUSE"
    assert isinstance(pause["executed"], dict)
    assert pause["executed"]["chassis"]["ok"] is True
    stopped = api.stop({"session_id": created["session_id"], "reason": "用户停止"})
    assert stopped["stopped"]["chassis"]["ok"] is True
    closed = api.close_session({"session_id": created["session_id"]})
    assert closed["ok"] is True


def test_shotpilot_dry_run_accepts_unknown_reachability(robot):
    api, created = session(robot, dry_run=True)
    started = api.start_shot({
        "session_id": created["session_id"], "host": robot,
        "confirm": True, "reachability": "UNKNOWN",
    })
    assert started["ok"] is True and started["dry_run"] is True
