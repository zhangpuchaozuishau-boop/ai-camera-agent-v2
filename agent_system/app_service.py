"""App-facing service: the phone is the entry point.

One flow, all of it reachable over WiFi:

    create_session(user_request)      -> storyboard (Planner V2, 1 LLM call)
    start_shot(session, shot_id)      -> initial plan executed on the rig
    observe(session, frames[...])     -> green box in, CONTINUE/ADJUST/PAUSE + commands out
    advance(session) / stop(session)  -> next shot, or halt

Transport-agnostic on purpose: the console server binds these to HTTP, tests call
them directly. Nothing here decides camera geometry itself - it delegates to
Planner V2, Feedback V2, DustCarMotionCompiler and the corrections compiler, and
keeps the session/clock bookkeeping the app needs.
"""

import threading
import time
import uuid

from .errors import AgentError
from .hardware import DustCarCalibration, VisionLoop, dustcar_registry
from .hardware.calibration_spec import field_state
from .mocks import MockReachabilityValidator
from .models import UserRequest
from .reachability import ReachabilityResult
from .storyboard import (ANGLE_ZH, MOVE_ZH, SHOT_SIZE_ZH, StoryboardConfig, StoryboardPlanner,
                         consistency_warnings, timeline, to_shot_script)

APP_API_VERSION = "app-api-1.0"
DEFAULT_MAX_SHOTS = 8
DEFAULT_MAX_SHOT_DURATION = 15.0
DEFAULT_MAX_TOTAL_DURATION = 60.0


def _error(code, reason, **context):
    return AgentError(code, reason, context or None)


def _number(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("INVALID_PARAMETERS", f"{name} 必须是数字", value=value)
    return float(value)


class RobotProbe:
    """Robot status behind a HARD time budget.

    The app allows 1.5s to read a reply and 3s in total, so hello must never wait on
    an offline robot: probing happens in a background thread that fills a cache, and
    hello returns the cache - or "checking" - immediately. A phone that asks again a
    second later gets the cached verdict instead of another stall.
    """

    def __init__(self, probe, *, wait_s=0.9, cache_s=10.0, fail_cache_s=30.0):
        self._probe = probe
        self._wait_s = wait_s
        self._cache_s = cache_s          # a reachable robot is re-checked after this
        self._fail_cache_s = fail_cache_s  # a DEAD robot is not re-probed for this long
        self._lock = threading.Lock()
        self._thread = None
        self.host = None
        self.status = None
        self.error = None
        self.checked_at = 0.0
        self.checks = 0

    def _run(self, host):
        try:
            status = self._probe(host)
            outcome = {"status": status, "error": None}
        except AgentError as error:
            outcome = {"status": None, "error": {"code": error.code, "reason": error.reason}}
        except Exception as error:                       # transport failure, timeout, ...
            outcome = {"status": None, "error": {"code": "DEVICE_UNREACHABLE", "reason": str(error)}}
        with self._lock:
            self.status, self.error = outcome["status"], outcome["error"]
            self.checked_at, self.host, self._thread = time.time(), host, None
            self.checks += 1

    def start(self, host):
        with self._lock:
            ttl = self._fail_cache_s if self.error is not None else self._cache_s
            stale = self.host != host or (time.time() - self.checked_at) > ttl
            if self._thread is None and stale:
                self._thread = threading.Thread(target=self._run, args=(host,), daemon=True)
                self._thread.start()
                return True
        return False

    def snapshot(self, host, wait_s=None):
        started = self.start(host)
        thread = None
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(self._wait_s if wait_s is None else max(0.0, float(wait_s)))
        with self._lock:
            fresh = self.host == host and self.checked_at > 0
            busy = self._thread is not None
            if not fresh:
                return {"reachable": None, "state": "checking" if busy else "unknown",
                        "status": None, "error": None, "checked_ago_s": None,
                        "started_probe": started,
                        "hint": "机器人探测仍在后台进行；稍后再调一次 hello 就能拿到结果"}
            return {"reachable": self.error is None, "state": "ready" if self.error is None else "unreachable",
                    "status": self.status, "error": self.error,
                    "checked_ago_s": round(time.time() - self.checked_at, 2),
                    "started_probe": started, "checks": self.checks}


class AppSession:
    def __init__(self, *, text, board, script, host, subject_height_mm, speed_pct, dry_run,
                 max_corrections, max_age_seconds, capability):
        self.session_id = "sess-" + uuid.uuid4().hex[:8]
        self.text = text
        self.board = board
        self.script = script
        self.host = host
        self.subject_height_mm = subject_height_mm
        self.speed_pct = speed_pct
        self.dry_run = dry_run
        self.max_corrections = max_corrections
        self.max_age_seconds = max_age_seconds
        self.capability = capability
        self.clock_offset = None
        self.index = 0
        self.loop = None
        self.plan_id = None
        self.created_at = time.time()
        self.decisions = []
        self.frames = 0
        self.closed = False

    @property
    def current_shot(self):
        return self.script.shots[self.index] if self.index < len(self.script.shots) else None

    def clock(self):
        return time.time()          # same domain as VisionLoop's default clock

    def to_agent_time(self, captured):
        """Map the phone's clock into the agent's clock.

        The NEWEST frame of a batch sets the offset: it is the one that just happened,
        so older frames in the same batch stay in the past instead of jumping into the
        future. The app never has to know the agent's clock.
        """
        if captured is None:
            return None
        if self.clock_offset is None:
            self.clock_offset = self.clock() - float(captured)
        return float(captured) + self.clock_offset

    def sync_clock(self, timestamps):
        """Align the phone's time base using the newest timestamp in the batch."""
        values = [float(value) for value in timestamps if value is not None]
        if values and self.clock_offset is None:
            self.clock_offset = self.clock() - max(values)
        return self.clock_offset

    def summary(self):
        same_shot = self.loop.summary() if self.loop else None
        return {"session_id": self.session_id, "text": self.text, "shots": len(self.script.shots),
                "current_index": self.index,
                "current_shot_id": self.current_shot.shot_id if self.current_shot else None,
                "plan_id": self.plan_id, "dry_run": self.dry_run, "frames": self.frames,
                "decisions": self.decisions, "loop": same_shot,
                "clock_offset": None if self.clock_offset is None else round(self.clock_offset, 3)}


class AppService:
    """Sessions + the shot cursor. Injected factories keep it testable."""

    def __init__(self, *, planner_factory, client_factory, robot_probe=None, probe_wait_s=0.9, journal=None):
        self._planner_factory = planner_factory
        self._client_factory = client_factory
        self._robot_probe = robot_probe
        self._journal = journal          # 可选：把手机发来的需求落盘，重启也不丢
        self.probe = RobotProbe(robot_probe, wait_s=probe_wait_s) if robot_probe is not None else None
        self.sessions = {}

    def summaries(self):
        """What the phone asked for, in one read-only list (debugging aid)."""
        rows = []
        for session in self.sessions.values():
            shots = list(getattr(session.board, "shots", []) or [])
            loop = getattr(session, "loop", None)
            rows.append({
                "session_id": session.session_id, "text": session.text,
                "mode": getattr(session, "mode", None), "host": session.host,
                "dry_run": session.dry_run, "shots": len(shots),
                "shot_ids": [shot.shot_id for shot in shots],
                "index": session.index,
                "current_shot": shots[session.index].shot_id if 0 <= session.index < len(shots) else None,
                "shot_started": bool(getattr(session, "plan_id", None)),
                "decisions": len(getattr(loop, "decisions", []) or []),
                "created_ago_s": round(time.time() - session.created_at, 1),
            })
        return rows

    # -------------------------------------------------------------- helpers

    def _session(self, body):
        session_id = body.get("session_id")
        session = self.sessions.get(session_id)
        if session is None:
            raise _error("NO_SESSION", "没有这个会话，请先 POST /api/app/session 创建",
                         session_id=session_id, known=list(self.sessions)[:5])
        return session

    def _live_session(self, body):
        session = self._session(body)
        if session.loop is None:
            raise _error("SHOT_NOT_STARTED", "这个会话还没有启动任何镜头，请先 POST /api/app/session/shot/start")
        return session

    def _config(self, body):
        return StoryboardConfig(
            max_shots=int(body.get("max_shots") or DEFAULT_MAX_SHOTS),
            max_shot_duration=float(body.get("max_shot_duration") or DEFAULT_MAX_SHOT_DURATION),
            max_total_duration=float(body.get("max_total_duration") or DEFAULT_MAX_TOTAL_DURATION),
            allowed_moves=body.get("allowed_moves") or None)

    # ---------------------------------------------------------------- hello

    def hello(self, body=None):
        started = time.time()
        body = body or {}
        calibration = DustCarCalibration.load()
        fields = field_state()
        measured = [row for row in fields if row["measured"]]
        robot = {"host": body.get("host") or "192.168.4.1", "reachable": None, "state": "not_requested",
                 "status": None, "error": None}
        if body.get("probe"):
            if self.probe is None:
                robot.update({"state": "unsupported", "hint": "这一侧没有配置机器人探测"})
            else:
                robot.update(self.probe.snapshot(robot["host"], wait_s=body.get("probe_wait_s")))
                robot["host"] = robot["host"]
        return {
            "ok": True, "api_version": APP_API_VERSION, "agent": "ai-camera-agent-v2",
            "now": time.time(),
            "timings": {"total_ms": int((time.time() - started) * 1000),
                        "probe_wait_s": None if self.probe is None else self.probe._wait_s,
                        "note": "hello 不会等待离线的机器人：探测在后台跑，这里最多等 probe_wait_s"},
            "calibration": {
                "ready": len(measured) == len(fields),
                "measured": len(measured), "total": len(fields),
                "missing": [row["key"] for row in fields if not row["measured"]],
                "note": "未标定的常量仍是 placeholder：距离/角度的数值不可信，但链路可用"},
            "capabilities": {
                "shot_sizes": list(SHOT_SIZE_ZH), "moves": list(MOVE_ZH),
                "limits": {
                    "max_shots": DEFAULT_MAX_SHOTS,
                    "max_shot_duration_s": DEFAULT_MAX_SHOT_DURATION,
                    "max_total_duration_s": DEFAULT_MAX_TOTAL_DURATION,
                    "max_single_move_mm": calibration.data["limits"]["max_single_move_mm"]["value"],
                    "max_speed_pct": calibration.data["limits"]["max_speed_pct"]["value"],
                    "max_run_seconds": calibration.data["limits"]["max_run_seconds"]["value"],
                    "max_correction_mm": calibration.data["limits"]["max_correction_mm"]["value"],
                    "max_correction_deg": calibration.data["limits"]["max_correction_deg"]["value"],
                    "min_correction_interval_s": calibration.data["limits"]["min_correction_interval_s"]["value"],
                },
                "registry_revision": dustcar_registry().revision},
            "robot": robot,
            "sessions": len(self.sessions),
            "flow": ["POST /api/app/session", "POST /api/app/session/shot/start",
                     "POST /api/app/session/observe (每帧/批量)", "POST /api/app/session/stop"],
        }

    def ping(self, body=None):
        """Connectivity check the app can call cheaply: no robot probe, no file scan."""
        return {"ok": True, "api_version": APP_API_VERSION, "agent": "ai-camera-agent-v2",
                "now": time.time(), "sessions": len(self.sessions),
                "robot_state": None if self.probe is None else
                ("unknown" if not self.probe.checked_at else
                 ("ready" if self.probe.error is None else "unreachable"))}

    # -------------------------------------------------------------- session

    def create_session(self, body):
        text = str(body.get("text") or "").strip()
        if not text:
            raise _error("INVALID_PARAMETERS", "text（用户需求）不能为空")
        config = self._config(body)
        board = self._planner_factory(text=text, config=config, mode=body.get("mode", "real"),
                                      model=body.get("model"), subject_height_mm=body.get("subject_height_mm"))
        session = AppSession(text=text, board=board, script=to_shot_script(board, dustcar_registry().revision),
                             host=str(body.get("host") or "192.168.4.1"),
                             subject_height_mm=float(body.get("subject_height_mm") or
                                                     DustCarCalibration.load().data["subject"]["default_height_mm"]["value"]),
                             speed_pct=int(body.get("speed_pct") or 40),
                             dry_run=bool(body.get("dry_run")),
                             max_corrections=int(body.get("max_corrections") or 3),
                             max_age_seconds=float(body.get("max_age_seconds") or 1.0),
                             capability=body.get("capability", "ALL"))
        self.sessions[session.session_id] = session
        if self._journal is not None:
            self._journal({"kind": "create_session", "session_id": session.session_id, "text": text,
                           "mode": body.get("mode", "real"), "host": session.host,
                           "subject_height_mm": session.subject_height_mm, "speed_pct": session.speed_pct,
                           "dry_run": session.dry_run, "shots": [shot.shot_id for shot in board.shots],
                           "board_title": board.title, "overall_goal": board.overall_goal})
        rows = timeline(board)
        return {"ok": True, "session_id": session.session_id,
                "storyboard": board.model_dump(), "warnings": consistency_warnings(board),
                "total_duration": rows[-1]["end"] if rows else 0.0,
                "shots": [{"index": i, "shot_id": shot.shot_id, "title": shot.title, "purpose": shot.purpose,
                           "frame_description": shot.frame_description,
                           "shot_size": shot.shot_size, "shot_size_zh": SHOT_SIZE_ZH[shot.shot_size],
                           "camera_angle": shot.camera_angle, "camera_angle_zh": ANGLE_ZH[shot.camera_angle],
                           "camera_move": shot.camera_move, "camera_move_zh": MOVE_ZH[shot.camera_move],
                           "move_speed": shot.move_speed, "transition_in": shot.transition_in,
                           "subject_action": shot.subject_action,
                           "start": row["start"], "end": row["end"], "duration": row["duration"],
                           "rig_hint": row["rig_hint"],
                           "target_curve": [{"t": key.time_offset,
                                             "center_x": key.frame_state.center_x,
                                             "center_y": key.frame_state.center_y,
                                             "subject_height_ratio": key.frame_state.subject_height_ratio,
                                             "distance": key.frame_state.distance}
                                            for key in shot.target_trajectory.keyframes],
                           "tolerance": shot.target_trajectory.tolerance.model_dump()}
                          for i, (shot, row) in enumerate(zip(board.shots, rows))],
                "subject_height_mm": session.subject_height_mm, "speed_pct": session.speed_pct,
                "host": session.host, "dry_run": session.dry_run}

    def session_state(self, body):
        session = self._session(body)
        payload = {"ok": True, "summary": session.summary()}
        if session.loop is not None:
            payload["target"] = session.loop.trajectory.evaluate(
                session.loop._trajectory_time(session.loop.clock())).model_dump()
            payload["servo_angles"] = session.loop.compiler.servo_angles
            payload["last_frame"] = session.loop.frames[-1] if session.loop.frames else None
        return payload

    def close_session(self, body):
        session = self._session(body)
        stopped = self._stop_loop(session, "session closed")
        session.closed = True
        self.sessions.pop(session.session_id, None)
        return {"ok": True, "session_id": session.session_id, "stopped": stopped,
                "summary": session.summary()}

    # ------------------------------------------------------------------ shot

    def start_shot(self, body):
        session = self._session(body)
        shot_id = body.get("shot_id")
        if shot_id is None:
            index = session.index
        else:
            ids = [shot.shot_id for shot in session.script.shots]
            if shot_id not in ids:
                raise _error("INVALID_PARAMETERS", "shot_id 不在这个分镜里", shot_id=shot_id, shots=ids)
            index = ids.index(shot_id)
        if index >= len(session.script.shots):
            raise _error("NO_MORE_SHOTS", "镜头已经拍完了", index=index, shots=len(session.script.shots))
        if body.get("dry_run") is not None:
            session.dry_run = bool(body["dry_run"])
        if body.get("host"):
            session.host = str(body["host"])
        if not session.dry_run and body.get("confirm") is not True:
            raise _error("CONFIRMATION_REQUIRED",
                         "真机会动：start_shot 需要 confirm=true，或先设 dry_run=true 演练")
        if session.loop is not None:
            self._stop_loop(session, "switching shot")

        shot = session.script.shots[index]
        client = self._client_factory(session.host, session.dry_run)
        loop = VisionLoop(client, shot=shot, registry=dustcar_registry(), calibration=DustCarCalibration.load(),
                          subject_height_mm=session.subject_height_mm, speed_pct=session.speed_pct,
                          max_corrections=session.max_corrections, max_age_seconds=session.max_age_seconds,
                          capability=session.capability, dry_run=session.dry_run)
        verdict = body.get("reachability", "REACHABLE")
        if session.dry_run and verdict == "UNKNOWN":
            verdict = "REACHABLE"
        plan = loop.start(MockReachabilityValidator(
            ReachabilityResult(status=verdict, reason="app request")))
        session.index = index
        session.loop = loop
        session.plan_id = loop.plan_id
        return {"ok": True, "session_id": session.session_id, "plan_id": loop.plan_id,
                "shot": {"index": index, "shot_id": shot.shot_id, "shot_goal": shot.shot_goal,
                         "expected_duration": shot.expected_duration},
                "plan": [{"action_name": action.action_name, "parameters": action.parameters}
                         for action in plan.actions],
                "dry_run": session.dry_run, "host": session.host,
                "frame_contract": FRAME_CONTRACT}

    def advance(self, body):
        session = self._session(body)
        stopped = self._stop_loop(session, "advance")
        session.index += 1
        if session.index >= len(session.script.shots):
            session.index = len(session.script.shots)
            return {"ok": True, "session_id": session.session_id, "finished": True, "stopped": stopped,
                    "summary": session.summary()}
        started = self.start_shot({**body, "shot_id": session.script.shots[session.index].shot_id})
        return {**started, "stopped_previous": stopped}

    # --------------------------------------------------------------- observe

    def observe(self, body):
        session = self._live_session(body)
        frames = body.get("frames")
        if frames is None:
            frames = [body]                    # single-frame shorthand
        if not isinstance(frames, list) or not frames:
            raise _error("INVALID_PARAMETERS", "frames 必须是非空数组")
        space = body.get("space")
        execute = body.get("execute", True)
        if body.get("clock_offset_s") is not None:
            session.clock_offset = float(body["clock_offset_s"])       # app declares its base
        elif body.get("reset_clock"):
            session.clock_offset = None                                # deliberate re-alignment
        else:
            session.sync_clock([frame.get("timestamp") for frame in frames if isinstance(frame, dict)])
        results = []
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                raise _error("INVALID_PARAMETERS", f"frames[{index}] 不是对象")
            captured = session.to_agent_time(frame.get("timestamp"))
            frame_space = frame.get("space", space)
            frame_width = frame.get("frame_width", body.get("frame_width"))
            frame_height = frame.get("frame_height", body.get("frame_height"))
            if frame.get("lost"):
                row = session.loop.lost(now=captured, execute=execute)
            else:
                box = frame.get("box") or frame
                for name in ("x1", "y1", "x2", "y2"):
                    if box.get(name) is None:
                        raise _error("INVALID_PARAMETERS",
                                     f"frames[{index}] 缺少 {name}（或传 lost=true 表示主体丢失）")
                row = session.loop.observe(x1=_number(box["x1"], "x1"), y1=_number(box["y1"], "y1"),
                                           x2=_number(box["x2"], "x2"), y2=_number(box["y2"], "y2"),
                                           frame_width=_number(frame_width, "frame_width"),
                                           frame_height=_number(frame_height, "frame_height"),
                                           timestamp=captured, now=captured,
                                           distance=_number(frame.get("distance"), "distance"),
                                           execute=execute, space=frame_space)
            decision = row["decision"]
            session.decisions.append(decision["decision"] if decision else row["gate"]["code"])
            session.frames += 1
            results.append({
                "trajectory_time": row["trajectory_time"], "age_s": row["age"],
                "gate": row["gate"], "target": row["target"], "box": row["box"],
                "decision": decision["decision"] if decision else None,
                "reason": decision["reason"] if decision else None,
                "correction_components": (decision or {}).get("correction_intent", {}).get("components")
                if decision and decision.get("correction_intent") else None,
                "corrections": row["corrections"], "executed": row["executed"], "deferred": row["deferred"],
            })
        hints = []
        for index, result in enumerate(results):
            gate = result["gate"] or {}
            if not gate.get("accepted") and "future" in str(gate.get("reason", "")):
                hints.append(f"帧{index} 的时间戳在未来（{result['age_s']}s）：请拍摄后立即上报，"
                             f"不要提前打时间戳；批量补传请用过去的时刻，Agent 首次会自动对齐手机时基")
            elif result["deferred"]:
                hints.append(f"帧{index} 的修正被推迟（最小间隔 {session.loop.budget.min_interval_s}s）")
        return {"ok": True, "session_id": session.session_id, "plan_id": session.plan_id,
                "results": results, "frames_total": session.frames, "hints": hints,
                "summary": session.summary()}

    def stop(self, body):
        session = self._live_session(body)
        stopped = self._stop_loop(session, body.get("reason") or "app stop")
        return {"ok": True, "session_id": session.session_id, "stopped": stopped,
                "summary": session.summary()}

    def _stop_loop(self, session, reason):
        if session.loop is None:
            return None
        stopped = session.loop.stop(reason)
        session.loop = None
        return stopped


FRAME_CONTRACT = {
    "box": "绿色框：x1,y1 = 左上，x2,y2 = 右下。像素坐标同时给 frame_width/frame_height；"
           "已归一化到 0..1 就不必给（也可用 space 显式指定）",
    "timestamp": "这一帧的拍摄时刻（秒，手机自己的单调时钟即可，Agent 首次自动对齐时基）",
    "lost": "主体不在画面里：{lost: true}，Feedback 会判 PAUSE 并物理停车",
    "distance": "可选，单位待团队确认；现在不参与决策",
    "space": "normalized | pixel（缺省按数值推断：>2 视为像素）",
    "batch": "一帧一个对象，也可以 frames:[...] 批量上报，按顺序判决",
    "contract": "越界或翻转的框会被拒（INVALID_OBSERVATION），不会被悄悄夹回合法范围",
    "rate": "建议 5-10 fps；修正最小间隔 0.4s，底盘保活 150-200ms 由 Agent 负责",
}
