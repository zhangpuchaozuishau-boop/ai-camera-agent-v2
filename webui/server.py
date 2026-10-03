"""Local interactive test console for Agent V2. Test harness only, not Agent core.

Real: DeepSeek Planner V2 call (OpenAIDirectorLLM + VisualPlanner), contracts,
validation, trajectory interpolation, Feedback V2 and the AgentState machine.
MOCK ONLY: Reachability verdict, Motion Compiler (the generated trajectory is
registered as the fixture at runtime), Executor and the observed bbox, which the
tester places by hand in the browser.

Run:  .venv/Scripts/python.exe -m webui.server  [--port 8765]
"""

import argparse
from collections import deque
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from agent_system.app_service import APP_API_VERSION, AppService
from agent_system.errors import AgentError
from agent_system.feedback import FeedbackConfig, evaluate_feedback_v2
from agent_system.llm import FakeDirectorLLM, OpenAIDirectorLLM
from agent_system.mocks import MockExecutor, MockReachabilityValidator, mock_correction_capabilities
from agent_system.models import Observation, ShotScript, UserRequest
from agent_system.motion_compiler import MockMotionCompiler, demo_registry
from agent_system.planner_v2 import (VISUAL_DIRECTOR_INSTRUCTIONS, VisualPlanner, VisualPlannerConfig,
                                     visual_capability, visual_director_schema)
from agent_system.reachability import ReachabilityResult
from agent_system.state import AgentState
from agent_system.demo_v2 import captain_script
from agent_system.hardware import (DustCarCalibration, DustCarClient, DustCarExecutor, DustCarMotionCompiler,
                                   DryRunClient, VisionLoop, dustcar_registry)
from agent_system.hardware.calibration_spec import (CALIBRATION_FIELDS, apply_values, field_state,
                                                    worksheet)
from agent_system.storyboard import (Storyboard, STORYBOARD_INSTRUCTIONS, FakeStoryboardLLM, OpenAIStoryboardLLM, StoryboardConfig,
                                     StoryboardPlanner, consistency_warnings, storyboard_schema, timeline, to_shot_script)

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent
RUNS = WEB / "runs"
SETTINGS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENT_OPENAI_MODEL")


def load_settings():
    """Same three settings the integration tests read: local .env, then process env."""
    config = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in SETTINGS:
                config[key] = value.strip().strip("\"'")
    for key in SETTINGS:
        if os.getenv(key):
            config[key] = os.getenv(key)
    return config


class TracingClient:
    """Wrap responses.create to keep request metadata, raw output, latency and usage.

    Never records headers or the API key.
    """

    def __init__(self, client):
        self.calls = []

        def create(**kwargs):
            started = time.perf_counter()
            entry = {"model": kwargs.get("model"), "input": kwargs.get("input"),
                     "strict": kwargs.get("text", {}).get("format", {}).get("strict")}
            try:
                response = client.responses.create(**kwargs)
            except Exception as exc:
                entry.update(latency_s=round(time.perf_counter() - started, 2),
                             error=f"{type(exc).__name__}: {str(exc)[:300]}")
                self.calls.append(entry)
                raise
            usage = getattr(response, "usage", None)
            entry.update(latency_s=round(time.perf_counter() - started, 2),
                         status=getattr(response, "status", None),
                         output_text=getattr(response, "output_text", None),
                         usage=usage.model_dump() if hasattr(usage, "model_dump") else None)
            self.calls.append(entry)
            return response

        from types import SimpleNamespace
        self.responses = SimpleNamespace(create=create)


SESSIONS = {}
LOCK = threading.Lock()


def _reachability(verdict):
    return MockReachabilityValidator(ReachabilityResult(status=verdict, reason="MOCK_ONLY_web_console_verdict"))


def _log_run(record):
    RUNS.mkdir(exist_ok=True)
    with open(RUNS / (time.strftime("%Y%m%d") + ".jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _error(exc):
    if isinstance(exc, AgentError):
        return {"code": exc.code, "reason": exc.reason, "context": exc.context}
    return {"code": type(exc).__name__, "reason": str(exc)[:500]}


def api_config(_body):
    settings = load_settings()
    registry = demo_registry()
    config = VisualPlannerConfig()
    capability = visual_capability(registry, config)
    return {"base_url": settings.get("OPENAI_BASE_URL"), "model": settings.get("AGENT_OPENAI_MODEL"),
            "api_key_present": bool(settings.get("OPENAI_API_KEY")),
            "planner_config": config.model_dump(), "capability": capability,
            "instructions": VISUAL_DIRECTOR_INSTRUCTIONS,
            "schema": visual_director_schema(capability, config),
            "storyboard_instructions": STORYBOARD_INSTRUCTIONS,
            "storyboard_config": StoryboardConfig().model_dump(),
            "storyboard_schema": storyboard_schema(StoryboardConfig())}


def api_plan(body):
    text = str(body.get("text", "")).strip()
    mode = body.get("mode", "real")
    verdict = body.get("reachability", "REACHABLE")
    allow_distance = bool(body.get("allow_distance", False))
    settings = load_settings()
    model = (body.get("model") or settings.get("AGENT_OPENAI_MODEL") or "").strip()
    registry = demo_registry()
    config = VisualPlannerConfig(allow_distance_targets=allow_distance)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "model": model, "text": text,
              "reachability": verdict, "allow_distance": allow_distance}
    started = time.perf_counter()
    tracer = None
    try:
        if not text:
            raise AgentError("SCHEMA_ERROR", "Request text is empty")
        if mode == "fixture":
            # Same scripted response the offline demo uses; 0 API calls.
            llm = FakeDirectorLLM([captain_script(registry.revision, distance_fixture=allow_distance).model_dump()])
            script = VisualPlanner(llm, config=config).plan(UserRequest(text=text), registry, _reachability(verdict))
        else:
            if not settings.get("OPENAI_API_KEY"):
                raise AgentError("CONFIG_ERROR", "OPENAI_API_KEY missing in .env")
            import openai
            with openai.OpenAI(api_key=settings["OPENAI_API_KEY"], base_url=settings.get("OPENAI_BASE_URL"),
                               max_retries=0, timeout=90.0) as client:
                tracer = TracingClient(client)
                llm = OpenAIDirectorLLM(tracer, model=model, instructions=VISUAL_DIRECTOR_INSTRUCTIONS)
                script = VisualPlanner(llm, config=config).plan(UserRequest(text=text), registry, _reachability(verdict))
        result = {"ok": True, "script": script.model_dump()}
    except Exception as exc:
        result = {"ok": False, "error": _error(exc)}
        if not isinstance(exc, AgentError):
            result["error"]["trace"] = traceback.format_exc(limit=3)[-800:]
    result.update(total_latency_s=round(time.perf_counter() - started, 2),
                  provider_calls=tracer.calls if tracer else [], mode=mode, model=model,
                  registry_revision=registry.revision)
    record.update({key: result.get(key) for key in ("ok", "script", "error", "total_latency_s", "provider_calls")})
    _log_run(record)
    return result


FIXTURE_STORYBOARD = {
    "schema_version": "storyboard-0.1", "title": "离线示例：青铜器展示", "overall_goal": "离线 fixture，0 次模型调用",
    "style": "沉稳", "subject": {"name": "青铜器", "subject_type": "fixed_object"}, "director_notes": None,
    "shots": [
        {"shot_id": "s1", "title": "建立", "purpose": "交代主体", "frame_description": "青铜器居中偏下，远景",
         "duration": 4.0, "shot_size": "wide", "camera_angle": "low", "camera_move": "push_in", "move_speed": "slow",
         "subject_action": None, "transition_in": "fade_in",
         "target_trajectory": {"keyframes": [
             {"time_offset": 0.0, "frame_state": {"center_x": 0.5, "center_y": 0.6, "subject_height_ratio": 0.3, "distance": None}},
             {"time_offset": 4.0, "frame_state": {"center_x": 0.5, "center_y": 0.55, "subject_height_ratio": 0.5, "distance": None}}],
             "tolerance": {"center_x_tolerance": 0.05, "center_y_tolerance": 0.05, "height_ratio_tolerance": 0.05, "distance_tolerance": None}}},
        {"shot_id": "s2", "title": "细节", "purpose": "纹饰特写", "frame_description": "纹饰充满画面",
         "duration": 3.0, "shot_size": "close_up", "camera_angle": "eye_level", "camera_move": "static", "move_speed": "slow",
         "subject_action": None, "transition_in": "cut",
         "target_trajectory": {"keyframes": [
             {"time_offset": 0.0, "frame_state": {"center_x": 0.5, "center_y": 0.5, "subject_height_ratio": 0.8, "distance": None}},
             {"time_offset": 3.0, "frame_state": {"center_x": 0.5, "center_y": 0.5, "subject_height_ratio": 0.8, "distance": None}}],
             "tolerance": {"center_x_tolerance": 0.05, "center_y_tolerance": 0.05, "height_ratio_tolerance": 0.05, "distance_tolerance": None}}},
    ]}


def plan_storyboard(text, config, mode="real", model="", calls=None):
    """One storyboard, real (DeepSeek) or offline fixture. `calls` collects provider calls."""
    if mode == "fixture":
        return StoryboardPlanner(FakeStoryboardLLM([{"storyboard": FIXTURE_STORYBOARD}]), config=config).plan(UserRequest(text=text))
    settings = load_settings()
    if not settings.get("OPENAI_API_KEY"):
        raise AgentError("CONFIG_ERROR", "OPENAI_API_KEY missing in .env")
    import openai
    with openai.OpenAI(api_key=settings["OPENAI_API_KEY"], base_url=settings.get("OPENAI_BASE_URL"),
                       max_retries=1, timeout=120.0) as client:
        tracer = TracingClient(client)
        board = StoryboardPlanner(OpenAIStoryboardLLM(tracer, model=model), config=config).plan(UserRequest(text=text))
    if calls is not None:
        calls.extend(tracer.calls)
    return board


def api_storyboard(body):
    text = str(body.get("text", "")).strip()
    mode = body.get("mode", "real")
    settings = load_settings()
    model = (body.get("model") or settings.get("AGENT_OPENAI_MODEL") or "").strip()
    config = StoryboardConfig(max_shots=int(body.get("max_shots", 8)),
                              max_shot_duration=float(body.get("max_shot_duration", 15)),
                              max_total_duration=float(body.get("max_total_duration", 60)),
                              allowed_moves=body.get("allowed_moves") or None)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "storyboard", "mode": mode, "model": model, "text": text}
    started = time.perf_counter()
    calls = []
    try:
        if not text:
            raise AgentError("SCHEMA_ERROR", "Request text is empty")
        board = plan_storyboard(text, config, mode, model, calls)
        registry = demo_registry()
        result = {"ok": True, "storyboard": board.model_dump(), "timeline": timeline(board),
                  "total_duration": timeline(board)[-1]["end"], "warnings": consistency_warnings(board),
                  "script": to_shot_script(board, registry.revision).model_dump()}
    except Exception as exc:
        result = {"ok": False, "error": _error(exc)}
        if not isinstance(exc, AgentError):
            result["error"]["trace"] = traceback.format_exc(limit=3)[-800:]
    result.update(total_latency_s=round(time.perf_counter() - started, 2),
                  provider_calls=calls, mode=mode, model=model)
    record.update({k: result.get(k) for k in ("ok", "storyboard", "error", "warnings", "total_latency_s", "provider_calls")})
    _log_run(record)
    return result


def BOARD_RENDER(record):
    if not record.get("storyboard"):
        return []
    board = Storyboard.model_validate(record["storyboard"])
    rows = timeline(board)
    for row in rows:
        row["total_duration"] = rows[-1]["end"]
    return rows


def api_last(body):
    """Most recent storyboard run from today's log, so a restart does not lose the result."""
    if not RUNS.is_dir():
        return {"ok": False, "error": {"code": "NO_RUNS", "reason": "还没有任何调用记录"}}
    for path in sorted(RUNS.glob("*.jsonl"), reverse=True):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            record = json.loads(line)
            if record.get("kind") == "storyboard":
                return {"ok": record["ok"], "mode": record.get("mode"), "model": record.get("model"),
                        "total_latency_s": record.get("total_latency_s"), "provider_calls": record.get("provider_calls") or [],
                        "storyboard": record.get("storyboard"), "warnings": record.get("warnings") or [],
                        "error": record.get("error"), "total_duration": BOARD_RENDER(record)[-1]["end"] if record.get("storyboard") else None,
                        "timeline": BOARD_RENDER(record),
                        "script": to_shot_script(Storyboard.model_validate(record["storyboard"]),
                                                 demo_registry().revision).model_dump() if record.get("storyboard") else None}
    return {"ok": False, "error": {"code": "NO_RUNS", "reason": "还没有分镜记录"}}


# ---------------------------------------------------------------- hardware

HW_SESSIONS = {}
DEFAULT_HOST = "192.168.4.1"


def _hw_client(host, timeout=2.0):
    host = (host or DEFAULT_HOST).strip()
    if host.startswith("http://"):
        host = host[len("http://"):]
    name, _, port = host.partition(":")
    return DustCarClient(name, port=int(port or 80), timeout=timeout)


def _hw_compile(body):
    """Visual target trajectory -> real actuator commands. Always dry: nothing is sent."""
    script = ShotScript.model_validate(body["script"])
    shot = next(s for s in script.shots if s.shot_id == body["shot_id"])
    registry = dustcar_registry()
    calibration = DustCarCalibration.load()
    context = AgentState(plan_id=body.get("plan_id") or "hw-" + uuid4().hex[:8], shot_id=shot.shot_id,
                         registry_revision=registry.revision)
    compiler = DustCarMotionCompiler(calibration, subject_height_mm=body.get("subject_height_mm"),
                                     speed_pct=body.get("speed_pct"))
    reachability = _reachability(body.get("reachability", "REACHABLE"))
    plan = compiler.compile(shot.target_trajectory, context, registry, reachability)
    commanded_ms = 0
    for action in plan.actions:
        if action.action_name == "chassis_drive":
            commanded_ms += action.parameters["duration_ms"]
        elif action.action_name == "stepper_run":
            commanded_ms += action.parameters["duration_ms"]
    limit_ms = int(calibration.value("limits", "max_run_seconds") * 1000)
    return {"plan": plan.model_dump(), "notes": compiler.notes, "context": context.model_dump(),
            "actions": [{"action_name": a.action_name, "parameters": a.parameters} for a in plan.actions],
            "commanded_seconds": round(commanded_ms / 1000.0, 2), "max_run_seconds": limit_ms / 1000.0,
            "within_run_limit": commanded_ms <= limit_ms,
            "unverified_constants": calibration.unverified(),
            "registry_revision": registry.revision}


def api_hw_config(_body):
    calibration = DustCarCalibration.load()
    registry = dustcar_registry()
    return {"default_host": DEFAULT_HOST, "registry_revision": registry.revision,
            "actions": {name: entry.description for name, entry in registry.actions.items()},
            "calibration": calibration.data, "unverified": calibration.unverified(),
            "calibration_fields": CALIBRATION_FIELDS,
            "calibration_form": field_state(),
            "limits": calibration.data["limits"]}


def api_hw_plan(body):
    """Dry run: compile only, no HTTP to the device."""
    result = _hw_compile(body)
    result["ok"] = True
    client = _hw_client(body.get("host"))
    result["target"] = client.base_url
    _log_run({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "hw_plan", "shot_id": body.get("shot_id"),
              "actions": result["actions"], "commanded_seconds": result["commanded_seconds"]})
    return result


def api_hw_run(body):
    """LIVE: compile then drive the robot. Requires an explicit confirm flag."""
    if body.get("confirm") is not True:
        raise AgentError("CONFIRMATION_REQUIRED", "Live motion needs confirm=true")
    compiled = _hw_compile(body)
    if not compiled["within_run_limit"]:
        raise AgentError("RUN_LIMIT", "Plan exceeds the adapter's run-time guard",
                         {"commanded_seconds": compiled["commanded_seconds"]})
    from agent_system.models import ShotExecutionPlan
    plan = ShotExecutionPlan.model_validate(compiled["plan"])
    registry = dustcar_registry()
    client = _hw_client(body.get("host"))
    executor = DustCarExecutor(client, registry)
    accepted = executor.submit_plan(plan)
    running = executor.start(plan.plan_id)
    with LOCK:
        HW_SESSIONS[plan.plan_id] = {"executor": executor, "client": client, "host": client.base_url,
                                    "action_log": executor.action_log, "events": executor.events}
    _log_run({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "hw_run", "host": client.base_url,
              "shot_id": body.get("shot_id"), "plan_id": plan.plan_id, "actions": compiled["actions"]})
    return {"ok": True, "plan_id": plan.plan_id, "host": client.base_url, "actions": compiled["actions"],
            "notes": compiled["notes"], "events": [e.model_dump() for e in (accepted, running)]}


def api_hw_state(body):
    with LOCK:
        session = HW_SESSIONS.get(body.get("plan_id"))
    if session is None:
        raise AgentError("NO_SESSION", "No hardware session with that id")
    executor, client = session["executor"], session["client"]
    return {"ok": True, "plan_id": body["plan_id"], "is_running": executor.is_running,
            "events": [e.model_dump() for e in executor.events], "action_log": executor.action_log,
            "requests": [{"method": m, "path": path, "payload": payload, "response": response}
                         for m, path, payload, response in client.requests],
            "error": None if executor.error is None else {"code": executor.error.code, "reason": executor.error.reason}}


def api_hw_stop(body):
    """Physical stop: /api/stop for actuators AND dir=stop for the chassis."""
    client = _hw_client(body.get("host"))
    stopped = {}
    for key, call in (("chassis", client.chassis_stop), ("actuator", lambda: client.stop("all", bool(body.get("emergency"))))):
        try:
            stopped[key] = call()
        except AgentError as error:
            stopped[key] = {"error": {"code": error.code, "reason": error.reason}}
    with LOCK:
        for session in HW_SESSIONS.values():
            if session["executor"].is_running:
                session["executor"].abort(reason="Console stop")
    _log_run({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "hw_stop", "host": client.base_url, "result": stopped})
    return {"ok": True, "host": client.base_url, "result": stopped}


def api_hw_poll(body):
    client = _hw_client(body.get("host"), timeout=1.5)
    try:
        return {"ok": True, "host": client.base_url, "status": client.status(), "actuator": client.actuator()}
    except AgentError as error:
        return {"ok": False, "host": client.base_url, "error": {"code": error.code, "reason": error.reason}}


# ------------------------------------------------------- vision closed loop

LOOPS = {}


def _loop_client(body):
    if body.get("dry_run"):
        return DryRunClient(), True
    if body.get("confirm") is not True:
        raise AgentError("CONFIRMATION_REQUIRED", "A live loop needs confirm=true")
    return _hw_client(body.get("host")), False


def api_loop_start(body):
    script = ShotScript.model_validate(body["script"])
    shot = next(s for s in script.shots if s.shot_id == body["shot_id"])
    client, dry = _loop_client(body)
    registry = dustcar_registry()
    loop = VisionLoop(client, shot=shot, registry=registry, calibration=DustCarCalibration.load(),
                      subject_height_mm=body.get("subject_height_mm"), speed_pct=body.get("speed_pct"),
                      max_corrections=int(body.get("max_corrections", 3)),
                      max_age_seconds=float(body.get("max_age_seconds", 1.0)),
                      capability=body.get("capability", "ALL"), dry_run=dry)
    plan = loop.start(_reachability(body.get("reachability", "REACHABLE")))
    with LOCK:
        LOOPS[loop.plan_id] = loop
    _log_run({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "loop_start", "plan_id": loop.plan_id,
              "dry_run": dry, "shot_id": shot.shot_id,
              "actions": [{"action_name": a.action_name, "parameters": a.parameters} for a in plan.actions]})
    return {"ok": True, "loop_id": loop.plan_id, "dry_run": dry,
            "plan": [{"action_name": a.action_name, "parameters": a.parameters} for a in plan.actions],
            "frame_contract": {
                "box": "绿色框的左上/右下 = x1,y1,x2,y2；像素坐标时同时给 frame_width/frame_height，"
                       "或直接给已归一化到 0..1 的值",
                "timestamp": "秒，与 gate 的 now 同一时钟域；超过 max_age_seconds 判 STALE 并丢弃",
                "plan_id/shot_id": "由本循环自动绑定；主体丢失请传 x1=y1=x2=y2=null 或 lost=true",
                "distance": "可选；你们确定内部单位后再启用"}}
 

def api_loop_observe(body):
    with LOCK:
        loop = LOOPS.get(body.get("loop_id"))
    if loop is None:
        raise AgentError("NO_SESSION", "Start a loop first")
    if body.get("lost"):
        row = _loop_lost(loop)
    else:
        row = loop.observe(x1=body["x1"], y1=body["y1"], x2=body["x2"], y2=body["y2"],
                           frame_width=body.get("frame_width"), frame_height=body.get("frame_height"),
                           timestamp=body.get("timestamp"), distance=body.get("distance"),
                           execute=body.get("execute", True), space=body.get("space"))
    row["summary"] = loop.summary()
    _log_run({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "loop_observe", "loop_id": loop.plan_id,
              "gate": row["gate"], "decision": row["decision"], "corrections": row["corrections"]})
    return {"ok": True, **row}


def _loop_lost(loop):
    from agent_system.models import Observation as _Observation
    from agent_system.feedback import evaluate_feedback_v2 as _evaluate

    now = loop.clock()
    observation = _Observation(shot_id=loop.shot.shot_id, plan_id=loop.plan_id, timestamp=now, bbox=None)
    gate, decision, state = _evaluate(loop.trajectory, loop._trajectory_time(now), observation, loop.state,
                                      loop.config, loop.capability, trajectory_plan_id=loop.plan_id, now=now)
    loop.state = state
    executed = loop._stop(decision.reason) if decision and decision.decision == "PAUSE" else None
    row = {"now": now, "box": None, "gate": {"accepted": gate.accepted, "code": gate.code, "reason": gate.reason},
           "decision": decision.model_dump() if decision else None, "corrections": None,
           "executed": executed, "deferred": None}
    loop.frames.append(row)
    return row


def api_loop_summary(body):
    with LOCK:
        loop = LOOPS.get(body.get("loop_id"))
    if loop is None:
        raise AgentError("NO_SESSION", "No loop with that id")
    return {"ok": True, "summary": loop.summary(),
            "requests": [{"method": m, "path": path, "payload": payload} for m, path, payload, _ in loop.client.requests],
            "frames": loop.frames}


def api_loop_stop(body):
    with LOCK:
        loop = LOOPS.get(body.get("loop_id"))
    if loop is None:
        raise AgentError("NO_SESSION", "No loop with that id")
    result = loop._stop("Console stop")
    return {"ok": True, "loop_id": loop.plan_id, "result": result, "summary": loop.summary()}


# ---------------------------------------------------------- calibration

def api_calibrate(body):
    """Write MEASURED constants into calibration.json (backed up first)."""
    if body.get("action") == "worksheet":
        return {"ok": True, "markdown": worksheet(), "path": "docs/CALIBRATION_WORKSHEET.md"}
    values = body.get("values") or {}
    if not values:
        raise AgentError("NO_VALUES", "没有要写入的常量")
    return apply_values(values, source=body.get("source") or ("measured via console " + time.strftime("%Y-%m-%d %H:%M")))


def api_session(body):
    """Compile one Shot with a runtime-registered mock fixture, then start mock execution."""
    script = ShotScript.model_validate(body["script"])
    shot = next(s for s in script.shots if s.shot_id == body["shot_id"])
    registry = demo_registry()
    verdict = body.get("reachability", "REACHABLE")
    plan_id = "web-" + uuid4().hex[:8]
    context = AgentState(plan_id=plan_id, shot_id=shot.shot_id, registry_revision=registry.revision)
    compiler = MockMotionCompiler(shot.target_trajectory, fixture_id="web-runtime-fixture")
    plan = compiler.compile(shot.target_trajectory, context, registry, _reachability(verdict))
    executor = MockExecutor(registry)
    state = context.mark_ready(plan, registry)
    events = [executor.submit_plan(plan)]
    state = state.on_executor_event(events[-1])
    events.append(executor.start(plan.plan_id))
    state = state.on_executor_event(events[-1])
    capability = body.get("correction_capability", "ALL")
    max_corrections = int(body.get("max_corrections", 3))
    with LOCK:
        SESSIONS[plan_id] = {"shot": shot, "plan": plan, "state": state, "capability": capability,
                             "config": FeedbackConfig(max_corrections=max_corrections, max_age_seconds=1.0),
                             "history": []}
    return {"session_id": plan_id, "plan": plan.model_dump(), "state": state.model_dump(),
            "events": [event.model_dump() for event in events]}


def api_feedback(body):
    with LOCK:
        session = SESSIONS.get(body.get("session_id"))
    if session is None:
        raise AgentError("NO_SESSION", "Start a feedback session first")
    shot, state = session["shot"], session["state"]
    t = float(body["t"])
    bbox = body.get("bbox")
    distance = body.get("distance")
    timestamp = 1000.0 + len(session["history"])  # explicit observation clock, same domain as now
    observation = Observation(shot_id=shot.shot_id, plan_id=session["plan"].plan_id, timestamp=timestamp,
                              bbox=bbox, distance=float(distance) if distance not in (None, "") else None)
    expected = shot.target_trajectory.evaluate(t)
    gate, decision, new_state = evaluate_feedback_v2(
        shot.target_trajectory, t, observation, state, session["config"],
        mock_correction_capabilities()[session["capability"]],
        trajectory_plan_id=session["plan"].plan_id, now=timestamp,
    )
    measured = observation.to_frame_state()
    stopped = None
    if decision is not None and decision.decision == "PAUSE" and body.get("auto_stop"):
        stopped = api_hw_stop({"host": body.get("host")})
    row = {"t": t, "expected": expected.model_dump(), "measured": measured.model_dump() if measured else None,
           "physical_stop": stopped,
           "gate": {"accepted": gate.accepted, "code": gate.code, "reason": gate.reason},
           "decision": decision.model_dump() if decision else None, "state": new_state.model_dump()}
    with LOCK:
        session["state"] = new_state
        session["history"].append(row)
    return row


def api_pause(body):
    """Simulate the executor confirming a PAUSE request, or resuming after it."""
    with LOCK:
        session = SESSIONS[body["session_id"]]
        state = session["state"]
        session["state"] = state.confirm_resume() if body.get("action") == "resume" else state.confirm_pause()
        return {"state": session["state"].model_dump()}


API = "/" + "api" + "/"
GET_PATHS = {"/", "/index.html", "/app.js", "/feedback.js", "/storyboard.js", "/hardware.js",
             "/vision.js", "/appsim.js"}          # console assets; API GETs added below
APP_LOG = deque(maxlen=40)   # recent /api/app/* hits from the phone
APP_TOKEN = ""          # set with --token / AGENT_APP_TOKEN to gate /api/app/*
PORT = 8765             # actual listening port, set by main(); used to build phone URLs

# ------------------------------------------------------------------- app API

CONSOLE_ASSETS = ("/", "/index.html", "/app.js", "/feedback.js", "/storyboard.js", "/hardware.js",
                  "/vision.js", "/appsim.js", "/favicon.ico")


def _note_app_request(handler, path, status, ms=None, error=None, note=None):
    """Log every NON-console request, whatever the outcome.

    A phone that posts to a wrong path, forgets the token or uses GET must still leave a
    trace here - otherwise "the phone never arrived" and "the phone arrived wrong" look
    identical from this side.
    """
    if path in CONSOLE_ASSETS or path.startswith("/runs/"):
        return
    entry = {"time": time.strftime("%H:%M:%S"), "method": handler.command, "path": path,
             "remote": handler.client_address[0], "status": status,
             "ms": None if ms is None else int(ms), "agent": (handler.headers.get("User-Agent") or "")[:60],
             "error": error, "note": note}
    APP_LOG.append(entry)
    if status != 200 or (ms is not None and ms > 1000):
        print(f"[app] {entry}", flush=True)


def _journal_session(record):
    """Append what the phone asked for, so a restart does not erase the record."""
    RUNS.mkdir(exist_ok=True)
    record = dict(record)
    record["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(RUNS / "app_sessions.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + chr(10))


def _app_planner(text, config, mode="real", model=None, subject_height_mm=None):
    """Planner factory for the app service: same construction the console uses."""
    settings = load_settings()
    return plan_storyboard(text, config, mode, model or settings.get("AGENT_OPENAI_MODEL") or "")


def _app_client(host, dry_run):
    return DryRunClient() if dry_run else _hw_client(host)


def _app_probe(host):
    """Probe with a SHORT timeout: the app's read budget is 1.5s, so the background
    probe must give up quickly instead of holding a socket for seconds."""
    return _hw_client(host, timeout=0.8).status()


APP = AppService(planner_factory=_app_planner, client_factory=_app_client, robot_probe=_app_probe,
                 journal=_journal_session)


def api_net(body):
    """Every address the phone could use, so the app dialog can list them."""
    return {"ok": True, "port": PORT,
            "candidates": [{"ip": "127.0.0.1", "url": f"http://127.0.0.1:{PORT}/api/app/", "for": "本机自测"}]
                          + [{"ip": ip, "url": f"http://{ip}:{PORT}/api/app/", "for": "手机（同一网段）"}
                             for ip in lan_addresses()],
            "how_to_pick": "手机 WiFi 的 IP 前三段要和这里某个地址一样；不一样说明不在同一网段。"}


def api_app_index(body=None):
    """The app's base URL itself must answer.

    Typing /api/app/ into a phone browser (or GETting it as a reachability probe,
    which some apps do) used to return 404 {"error": "not found"} - indistinguishable
    from "the agent is not there". Now it is a connectivity receipt.
    """
    return {"ok": True, "api_version": APP_API_VERSION, "agent": "ai-camera-agent-v2",
            "message": "Agent 在线：能看到这段 JSON，就说明手机到这台电脑的网络是通的",
            "tip": "把地址复制到手机浏览器打开也能验证；业务端点只收 POST + JSON body",
            "port": PORT, "host": "0.0.0.0",
            "probe": "GET/POST /api/app/ping（不探机器人，毫秒级）",
            "endpoints": {
                "ping": ["GET", "POST"],
                "hello": ["POST"], "session": ["POST"],
                "session/shot/start": ["POST"], "session/observe": ["POST"],
                "session/state": ["POST"], "session/advance": ["POST"],
                "session/stop": ["POST"], "session/close": ["POST"]},
            "docs": "docs/APP_INTEGRATION.md"}


def api_app_ping(body):
    return APP.ping(body)


def api_net_sessions(body):
    """The phone's live sessions: which requirement, which shot, how many decisions."""
    sessions = APP.summaries()
    return {"ok": True, "count": len(sessions), "sessions": sessions}


def api_applog(body):
    """Recent /api/app/* requests: lets you watch the phone arrive from this side."""
    return {"ok": True, "port": PORT, "entries": list(APP_LOG)}


def api_app_hello(body):
    return APP.hello(body)


def api_app_session(body):
    return APP.create_session(body)


def api_app_shot_start(body):
    return APP.start_shot(body)


def api_app_observe(body):
    return APP.observe(body)


def api_app_advance(body):
    return APP.advance(body)


def api_app_state(body):
    return APP.session_state(body)


def api_app_stop(body):
    return APP.stop(body)


def api_app_close(body):
    return APP.close_session(body)


ROUTES = {API + name: fn for name, fn in (
    ("config", api_config), ("plan", api_plan), ("session", api_session), ("feedback", api_feedback),
    ("pause", api_pause), ("storyboard", api_storyboard), ("last", api_last),
    ("hw/" + "config", api_hw_config), ("hw/" + "plan", api_hw_plan), ("hw/" + "run", api_hw_run),
    ("hw/" + "state", api_hw_state), ("hw/" + "stop", api_hw_stop), ("hw/" + "poll", api_hw_poll),
    ("hw/" + "loop/start", api_loop_start), ("hw/" + "loop/observe", api_loop_observe),
    ("hw/" + "loop/summary", api_loop_summary), ("hw/" + "loop/stop", api_loop_stop),
    ("hw/" + "calibrate", api_calibrate),
    ("net", api_net), ("app", api_app_index), ("net/applog", api_applog), ("net/sessions", api_net_sessions), ("app/ping", api_app_ping),
    ("app/" + "hello", api_app_hello),
    ("app/" + "session", api_app_session),
    ("app/" + "session/shot/start", api_app_shot_start),
    ("app/" + "session/observe", api_app_observe),
    ("app/" + "session/advance", api_app_advance),
    ("app/" + "session/state", api_app_state),
    ("app/" + "session/stop", api_app_stop),
    ("app/" + "session/close", api_app_close),
)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[web] " + fmt % args)

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")          # the phone app posts cross-origin
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _app_alias(self, path):
        """Tolerate an app that drops the /app segment: /api/ping -> /api/app/ping.

        Measured on a real phone (HarmonyOS, 172.20.10.3): it polls GET /api/ping
        while the documented path is /api/app/ping. Accepting the alias makes the
        app work as-is; the substitution is logged, not silent.
        """
        if path == API.rstrip("/"):                 # "/api" 本身 = App 入口索引
            return API + "app"
        if not path.startswith(API) or path.startswith(API + "app"):
            return None
        tail = path[len(API):]
        candidate = API + "app" + (("/" + tail) if tail else "")
        known = candidate in ROUTES or candidate in GET_PATHS or candidate == API + "app"
        return candidate if known else None

    def _normalized(self):
        """Strip a trailing slash, and say so in the log - tolerant, not silent."""
        path = "/" if self.path == "/" else (self.path.rstrip("/") or "/")
        if path != self.path:
            _note_app_request(self, self.path, 200, None, None,
                              f"路径带尾斜杠，已按 {path} 处理")
        return path

    def do_GET(self):
        static = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                  "/app.js": ("app.js", "text/javascript"), "/feedback.js": ("feedback.js", "text/javascript"),
                  "/storyboard.js": ("storyboard.js", "text/javascript"),
                  "/hardware.js": ("hardware.js", "text/javascript"),
                  "/vision.js": ("vision.js", "text/javascript"),
                  "/appsim.js": ("appsim.js", "text/javascript")}
        path = self._normalized()
        alias = self._app_alias(path)
        if alias is not None:
            _note_app_request(self, self.path, 200, None, None,
                              f"路径缺少 /app 段，已按 {alias} 处理（建议 App 基址用 /api/app/）")
            path = alias
        if path in static:
            name, kind = static[path]
            return self._send(200, (WEB / name).read_bytes(), kind + "; charset=utf-8")
        if path == API + "config":
            return self._send(200, api_config({}))
        if path == API + "last":
            return self._send(200, api_last({}))
        if path == API + "hw/" + "config":
            return self._send(200, api_hw_config({}))
        if path == API + "app":
            return self._send(200, api_app_index({}))
        if path == API + "app/ping":                          # the app's cheap reachability check
            return self._send(200, APP.ping({}))
        if path == API + "net/applog":
            return self._send(200, api_applog({}))
        if path == API + "net/sessions":
            return self._send(200, api_net_sessions({}))
        self._send(404, {"error": "not found"})
        _note_app_request(self, self.path, 404, None,
                          "GET 未命中（App 接口只收 POST；ping 例外）")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        hit = time.perf_counter()
        path = self._normalized()
        alias = self._app_alias(path)
        if alias is not None:
            _note_app_request(self, self.path, 200, None, None,
                              f"路径缺少 /app 段，已按 {alias} 处理（建议 App 基址用 /api/app/）")
            path = alias
        handler = ROUTES.get(path)
        if handler is None:
            self._send(404, {"error": "not found"})
            _note_app_request(self, self.path, 404, (time.perf_counter() - hit) * 1000,
                              "路径不在路由表（App 侧路径拼错？）")
            return
        if APP_TOKEN and self.path.startswith(API + "app/"):
            if self.headers.get("X-Agent-Token") != APP_TOKEN:
                self._send(401, {"ok": False, "error": {"code": "UNAUTHORIZED",
                                                       "reason": "缺少或错误的 X-Agent-Token"}})
                _note_app_request(self, self.path, 401, None, "token 不匹配")
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            started = time.perf_counter()
            payload = handler(body)
            self._send(200, payload)
            _note_app_request(self, self.path, 200, (time.perf_counter() - started) * 1000)
        except Exception as exc:
            self._send(400, {"ok": False, "error": _error(exc)})
            _note_app_request(self, self.path, 400, None, str(exc))


def lan_addresses():
    """Every IPv4 address the phone could reach this agent on."""
    import socket
    found = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.168.4.1", 80))            # no packet leaves the host
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(address for address in found if not address.startswith("127."))


def main():
    global APP_TOKEN, PORT
    parser = argparse.ArgumentParser(description="Agent V2 test console + app API")
    parser.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from the phone (default)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("AGENT_APP_TOKEN", ""),
                        help="需要时给 /api/app/* 加 X-Agent-Token 校验")
    args = parser.parse_args()
    APP_TOKEN = args.token.strip()
    PORT = args.port
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"调试台     http://127.0.0.1:{args.port}/")
    for address in lan_addresses():
        print(f"App 入口   http://{address}:{args.port}/api/app/   (手机填这个地址)")
    if APP_TOKEN:
        print("App 接口已开启 X-Agent-Token 校验")
    server.serve_forever()


if __name__ == "__main__":
    main()
