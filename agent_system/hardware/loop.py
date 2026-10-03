"""Live closed loop: the phone app's observations drive the rig.

One call to observe() is one control step:

    bbox (green box TL/BR)  -> Observation V1 -> Gate -> Feedback V2
                                                        |
                       CONTINUE  -> nothing this frame  |
                       ADJUST    -> CorrectionCompiler -> DustCarExecutor.execute_corrections
                       PAUSE     -> /api/stop + dir=stop (the rig stops)

Nothing here is open-loop guessing: every step consumes a fresh observation and the
gate rejects stale / mis-bound / out-of-range ones instead of moving on bad data.
The loop keeps the AgentState correction budget and the last commanded servo angles.
"""

import time
from uuid import uuid4

from ..errors import AgentError
from ..feedback import FeedbackConfig, evaluate_feedback_v2
from ..mocks import mock_correction_capabilities
from ..models import Observation, TargetTrajectory
from ..state import AgentState
from .compiler import DustCarMotionCompiler
from .corrections import CorrectionCompiler, budget_from
from .executor import DustCarExecutor


PIXEL_HINT = 2.0  # a real pixel coordinate is never this small


def normalize_box(x1, y1, x2, y2, *, frame_width=None, frame_height=None, space=None):
    """Vision module output -> normalized [0,1] corners.

    Accepts normalized input directly, or PIXEL top-left/bottom-right together with
    the frame size. Pass space="pixel"/"normalized" to be explicit; otherwise a box
    whose corners exceed 2.0 is read as pixels (a slightly out-of-frame normalized
    box must not silently switch units). Coordinates are never repaired: an
    out-of-frame or inverted box is refused, because clamping it would move the rig
    on false data.
    """
    values = [x1, y1, x2, y2]
    if any(not isinstance(v, (int, float)) or v != v for v in values):
        raise AgentError("INVALID_OBSERVATION", "bbox corners must be finite numbers", {"box": values})
    if space not in (None, "pixel", "normalized"):
        raise AgentError("INVALID_OBSERVATION", "space must be 'pixel' or 'normalized'", {"space": space})
    pixels = space == "pixel" or (space is None and max(abs(v) for v in values) > PIXEL_HINT)
    if pixels:
        if not frame_width or not frame_height:
            raise AgentError("INVALID_OBSERVATION", "pixel coordinates need frame_width/frame_height",
                             {"box": values})
        values = [x1 / frame_width, y1 / frame_height, x2 / frame_width, y2 / frame_height]
    x1, y1, x2, y2 = values
    if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
        raise AgentError("INVALID_OBSERVATION", "bbox outside the normalized frame or inverted",
                         {"x1": round(x1, 4), "y1": round(y1, 4), "x2": round(x2, 4), "y2": round(y2, 4)})
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


class VisionLoop:
    """One shot, one rig, one phone. Holds the state the feedback contract requires."""

    def __init__(self, client, *, shot, registry, calibration, subject_height_mm=None, speed_pct=None,
                 max_corrections=3, max_age_seconds=1.0, capability="ALL", clock=time.time, dry_run=False):
        self.client = client
        self.registry = registry
        self.calibration = calibration
        self.shot = shot
        self.trajectory = TargetTrajectory.model_validate(shot.target_trajectory.model_dump())
        self.plan_id = "loop-" + uuid4().hex[:8]
        self.config = FeedbackConfig(max_corrections=max_corrections, max_age_seconds=max_age_seconds)
        self.capability = mock_correction_capabilities()[capability]
        self.clock = clock
        self.dry_run = dry_run
        self.budget = budget_from(calibration)
        self.last_correction_at = None
        self.correction_count = 0
        self.executor = DustCarExecutor(client, registry, keepalive_ms=180)
        self.compiler = CorrectionCompiler(calibration, subject_height_mm=subject_height_mm, speed_pct=speed_pct)
        self.state = AgentState(plan_id=self.plan_id, shot_id=shot.shot_id, registry_revision=registry.revision)
        self.plan = None
        self.frames = []

    # -- startup -----------------------------------------------------------
    def start(self, reachability, *, pretend_executing=True):
        """Compile and submit the INITIAL plan, then mark the shot EXECUTING.

        pretend_executing=True is the rehearsal path used by tests and the console:
        the plan is validated but not driven, so the loop can be exercised without
        moving anything. Real runs go through the executor's own lifecycle.
        """
        from ..motion_compiler import require_reachable

        require_reachable(self.trajectory, reachability)
        compiler = DustCarMotionCompiler(self.calibration, subject_height_mm=self.compiler.subject_height_mm,
                                         speed_pct=self.compiler.speed_pct)
        context = AgentState(plan_id=self.plan_id, shot_id=self.shot.shot_id,
                             registry_revision=self.registry.revision)
        self.plan = compiler.compile(self.trajectory, context, self.registry, reachability)
        if not self.dry_run:
            self.executor.submit_plan(self.plan)
        self.state = AgentState(plan_id=self.plan_id, shot_id=self.shot.shot_id,
                                registry_revision=self.registry.revision, status="EXECUTING")
        return self.plan

    # -- one control step --------------------------------------------------
    def observe(self, *, x1, y1, x2, y2, frame_width=None, frame_height=None, timestamp=None,
                distance=None, execute=True, now=None, space=None, judged_at=None):
        """timestamp = when the frame was CAPTURED; now = its time in the agent's clock
        domain (drives the trajectory). Freshness is judged_at - timestamp where
        judged_at defaults to the real clock, so a delayed frame is rejected as STALE
        instead of being silently treated as current.
        """
        box = normalize_box(x1, y1, x2, y2, frame_width=frame_width, frame_height=frame_height, space=space)
        now = self.clock() if now is None else now
        captured = now if timestamp is None else timestamp
        observation = Observation(shot_id=self.shot.shot_id, plan_id=self.plan_id, timestamp=captured,
                                  bbox=box, distance=distance)
        judged = self.clock() if judged_at is None else judged_at
        gate, decision, state = evaluate_feedback_v2(
            self.trajectory, self._trajectory_time(now), observation, self.state, self.config,
            self.capability, trajectory_plan_id=self.plan_id, now=judged)
        target = self.trajectory.evaluate(self._trajectory_time(now))
        row = {"now": now, "captured": captured, "age": round(judged - captured, 3),
               "target": target.model_dump() if hasattr(target, "model_dump") else target,
               "trajectory_time": self._trajectory_time(now), "box": box,
               "gate": {"accepted": gate.accepted, "code": gate.code, "reason": gate.reason},
               "decision": decision.model_dump() if decision else None,
               "corrections": None, "executed": None, "deferred": None}
        self.state = state

        if decision is not None and decision.decision == "ADJUST":
            wait = self._seconds_until_correction_allowed(now)
            if wait > 0:
                row["deferred"] = f"距离上次修正仅 {round(now - self.last_correction_at, 2)}s，等待 {round(wait, 2)}s"
            elif not execute:
                row["deferred"] = "本次调用 execute=false"
            else:
                actions, notes = self.compiler.compile(decision.correction_intent, self.registry)
                row["corrections"] = notes
                if self.dry_run:
                    row["executed"] = [{"dry_run": True, "action_name": a.action_name,
                                        "parameters": a.parameters} for a in actions]
                else:
                    row["executed"] = self.executor.execute_corrections(actions)
                self.last_correction_at = now
                self.correction_count += 1
        elif decision is not None and decision.decision == "PAUSE":
            row["executed"] = self._stop(decision.reason)
        self.frames.append(row)
        return row

    def _seconds_until_correction_allowed(self, now):
        if self.last_correction_at is None:
            return 0.0
        return max(0.0, self.budget.min_interval_s - (now - self.last_correction_at))

    def _trajectory_time(self, now):
        """Map wall-clock seconds onto the trajectory: the shot starts when the loop does."""
        if not hasattr(self, "_started_at"):
            self._started_at = now
        return min(self.trajectory.duration, max(0.0, now - self._started_at))

    def _stop(self, reason):
        result = {"reason": reason}
        try:
            result["chassis"] = self.client.chassis_stop()
            result["actuator"] = self.client.stop("all", False)
        except AgentError as error:
            result["error"] = {"code": error.code, "reason": error.reason}
        return result

    def stop(self, reason="Stopped"):
        """Public stop: physical halt of chassis + actuators (used by the app API)."""
        return self._stop(reason)

    def lost(self, *, now=None, execute=True):
        """Report a frame with no subject box: Feedback decides PAUSE and we halt."""
        moment = self.clock() if now is None else now
        observation = Observation(shot_id=self.shot.shot_id, plan_id=self.plan_id, timestamp=moment, bbox=None)
        gate, decision, state = evaluate_feedback_v2(
            self.trajectory, self._trajectory_time(moment), observation, self.state, self.config,
            self.capability, trajectory_plan_id=self.plan_id, now=self.clock())
        self.state = state
        target = self.trajectory.evaluate(self._trajectory_time(moment))
        row = {"now": moment, "captured": moment, "age": round(self.clock() - moment, 3),
               "trajectory_time": self._trajectory_time(moment),
               "target": target.model_dump() if hasattr(target, "model_dump") else target, "box": None,
               "gate": {"accepted": gate.accepted, "code": gate.code, "reason": gate.reason},
               "decision": decision.model_dump() if decision else None,
               "corrections": None, "executed": None, "deferred": None}
        self.frames.append(row)
        if decision is not None and decision.decision == "PAUSE" and execute:
            row["executed"] = self._stop(decision.reason)
        return row

    def sync(self):
        """Resync commanded servo angles from E6 (another client may have moved them)."""
        return self.compiler.sync_servo_angles(self.client.actuator())

    def summary(self):
        decisions = [frame["decision"]["decision"] if frame["decision"] else frame["gate"]["code"] for frame in self.frames]
        return {"plan_id": self.plan_id, "frames": len(self.frames), "decisions": decisions,
                "corrections_executed": self.correction_count,
                "correction_budget": self.config.max_corrections,
                "final_state": self.state.model_dump(), "servo_angles": self.compiler.servo_angles,
                "requests": None if self.dry_run else len(self.client.requests)}
