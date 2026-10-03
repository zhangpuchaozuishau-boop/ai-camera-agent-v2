"""Visual target trajectory -> DustCar actuator commands.

Geometry (all angles from the pinhole model, same convention as the phone-vision-arm
rig model: u = cx0 - fx*tan(pan), v = cy0 + fy*tan(tilt)):

    centre_x -> pan angle   pan_deg = deg(atan((0.5 - center_x) * frame_width / fx))
    centre_y -> tilt angle  tilt_deg = deg(atan((center_y - 0.5) * frame_height / fy))
    height ratio -> distance  h = subject_height_mm * fy / (frame_height * D)
                              D = subject_height_mm * fy / (frame_height * h)

Time-based travel (the MKS steppers and the mecanum watchdog have no position
feedback in this firmware):
    chassis duration_ms = |d_mm| / (mm_per_s_at_100pct * speed/100) * 1000
    stepper duration_ms = |d_mm| / (mm_per_rev * rpm/60) * 1000

Every constant lives in calibration.json with its provenance. Placeholder
constants make the OUTPUT UNRELIABLE, not the arithmetic.
"""

from decimal import Decimal
import json
import math
from pathlib import Path

from pydantic import Field

from ..errors import AgentError
from ..models import ContractModel, ShotExecutionPlan, StructuredAction, TargetTrajectory
from ..motion_compiler import require_reachable
from ..registry import ActionRegistry
from ..state import AgentState
from ..validation import validate_plan

CALIBRATION_PATH = Path(__file__).resolve().parent / "calibration.json"
REGISTRY_PATH = Path(__file__).resolve().parent / "dustcar_registry.json"

CHASSIS_ACTIONS = {"chassis_drive", "chassis_stop"}
ACTUATOR_ACTIONS = {"stepper_run", "servo_set", "actuator_stop"}


class DustCarCalibration(ContractModel):
    """Loads calibration.json; `verified` flags are advisory, never used to fake values."""

    data: dict

    @classmethod
    def load(cls, path=None):
        return cls(data=json.loads(Path(path or CALIBRATION_PATH).read_text(encoding="utf-8")))

    def value(self, *keys):
        node = self.data
        for key in keys:
            node = node[key]
        return node["value"] if isinstance(node, dict) and "value" in node else node

    def unverified(self) -> list[str]:
        """Constants still marked verified=false, reported per FIELD.

        A group like servos.pan keeps its own `measured` map so a partially measured
        group still shows exactly which numbers are placeholders.
        """
        out = []

        def walk(node, prefix):
            if not isinstance(node, dict):
                return
            fields = [k for k, v in node.items() if not isinstance(v, (dict, list)) and k not in ("verified", "source", "note")]
            measured = node.get("measured") or {}
            scalar_group = "verified" in node or fields and ("value" not in node and "device" not in node)
            if "verified" in node:
                if not node["verified"]:
                    out.append(".".join(prefix) + f" = {node.get('value', {k: node[k] for k in fields})} "
                               f"({node.get('source', '')})")
                return
            if "device" in node:  # axes groups: recorded once you set them
                if not node.get("verified"):
                    out.append(".".join(prefix) + f".device = {node['device']} ({node.get('note', '')})")
                return
            for key, child in node.items():
                if key.startswith("_"):
                    continue
                if isinstance(child, dict) and "value" not in child and "device" not in child and "verified" not in child:
                    walk(child, prefix + [key])
                elif isinstance(child, dict):
                    walk(child, prefix + [key])
                elif key not in ("verified", "source", "note", "measured") and measured.get(key) is None:
                    out.append(".".join(prefix + [key]) + f" = {child} (未标定)")

        walk(self.data, [])
        return out

    # -- geometry ----------------------------------------------------------
    def pan_deg(self, center_x: float) -> float:
        return math.degrees(math.atan((0.5 - center_x) * self.value("camera", "frame_width_px")
                                      / self.value("camera", "fx_px")))

    def tilt_deg(self, center_y: float) -> float:
        return math.degrees(math.atan((center_y - 0.5) * self.value("camera", "frame_height_px")
                                      / self.value("camera", "fy_px")))

    def distance_mm(self, height_ratio: float, subject_height_mm: float | None = None) -> float:
        height_mm = subject_height_mm or self.value("subject", "default_height_mm")
        return height_mm * self.value("camera", "fy_px") / (self.value("camera", "frame_height_px") * height_ratio)

    # -- actuator conversions ---------------------------------------------
    def chassis_duration_ms(self, distance_mm: float, speed_pct: int) -> int:
        rate = self.value("chassis", "mm_per_s_at_100pct") * speed_pct / 100.0
        return int(round(abs(distance_mm) / rate * 1000.0))

    def servo_angle(self, joint: str, joint_deg: float) -> int:
        spec = self.data["servos"][joint]
        raw = spec["zero_angle"] + spec["sign"] * joint_deg * spec["deg_per_unit"]
        lo, hi = self.data["servos"]["safe_min_angle"]["value"], self.data["servos"]["safe_max_angle"]["value"]
        return int(round(min(hi, max(lo, raw))))

    def stepper_duration_ms(self, joint: str, travel_mm: float) -> tuple[int, int]:
        spec = self.data["steppers"][joint]
        direction = spec["dir_up"] if joint == "column" else spec["dir_extend"]
        rate_mm_per_s = spec["mm_per_rev"] * spec["rpm"] / 60.0
        return direction, int(round(abs(travel_mm) / rate_mm_per_s * 1000.0))


def dustcar_registry(path=None) -> ActionRegistry:
    return ActionRegistry.model_validate(json.loads(Path(path or REGISTRY_PATH).read_text(encoding="utf-8")))


class DustCarMotionCompiler:
    """Implements the MotionCompiler Protocol against the real firmware contract.

    One action per actuator change, all SEQUENTIAL (the frozen plan validator
    refuses PARALLEL). `notes` records the arithmetic for review.
    """

    def __init__(self, calibration: DustCarCalibration | None = None, *, subject_height_mm=None,
                 speed_pct=None, estop_hook=None):
        self.calibration = calibration or DustCarCalibration.load()
        self.subject_height_mm = subject_height_mm
        self.speed_pct = int(speed_pct or self.calibration.value("chassis", "default_speed_pct"))
        cap = int(self.calibration.value("limits", "max_speed_pct"))
        if not self.calibration.value("chassis", "min_speed_pct") <= self.speed_pct <= cap:
            raise AgentError("INVALID_PARAMETERS", "speed_pct outside firmware/safety range",
                             {"speed_pct": self.speed_pct, "allowed": [self.calibration.value("chassis", "min_speed_pct"), cap]})
        self.notes = []

    # -- one keyframe pair -> actions -------------------------------------
    def _segment_actions(self, left, right, context, registry, index):
        before, after = left.frame_state, right.frame_state
        travel = self.calibration.distance_mm(after.subject_height_ratio, self.subject_height_mm) - \
            self.calibration.distance_mm(before.subject_height_ratio, self.subject_height_mm)
        pan_from, pan_to = self.calibration.pan_deg(before.center_x), self.calibration.pan_deg(after.center_x)
        tilt_from, tilt_to = self.calibration.tilt_deg(before.center_y), self.calibration.tilt_deg(after.center_y)
        note = {"segment": index, "t0": left.time_offset, "t1": right.time_offset,
                "height_ratio": [before.subject_height_ratio, after.subject_height_ratio],
                "distance_mm": [round(self.calibration.distance_mm(before.subject_height_ratio, self.subject_height_mm), 1),
                                round(self.calibration.distance_mm(after.subject_height_ratio, self.subject_height_mm), 1)],
                "dolly_mm": round(travel, 1),
                "pan_deg": [round(pan_from, 2), round(pan_to, 2)],
                "tilt_deg": [round(tilt_from, 2), round(tilt_to, 2)], "actions": []}
        actions = []

        def add(name, parameters):
            action = StructuredAction(action_id=f"{context.shot_id}-{index}-{len(actions)}-{name}-{context.plan_id[:6]}",
                                      shot_id=context.shot_id, plan_id=context.plan_id, source="INITIAL",
                                      action_name=name, parameters=parameters,
                                      registry_revision=registry.revision)
            actions.append(action)
            note["actions"].append({"action_name": name, "parameters": parameters})
            return action

        # 1. orientation first: absolute servo targets for the end of this segment.
        if abs(self.calibration.servo_angle("pan", pan_to) - self.calibration.servo_angle("pan", pan_from)) > 0:
            add("servo_set", {"id": self.calibration.data["servos"]["pan"]["id"],
                              "angle": self.calibration.servo_angle("pan", pan_to)})
        if abs(self.calibration.servo_angle("tilt", tilt_to) - self.calibration.servo_angle("tilt", tilt_from)) > 0:
            add("servo_set", {"id": self.calibration.data["servos"]["tilt"]["id"],
                              "angle": self.calibration.servo_angle("tilt", tilt_to)})

        # 2. dolly: chassis translation over the segment's nominal time.
        limit = self.calibration.value("limits", "max_single_move_mm")
        if abs(travel) > limit:
            raise AgentError("UNREACHABLE_MOVE", "One segment asks for more travel than the safety limit",
                             {"dolly_mm": round(travel, 1), "limit_mm": limit})
        if abs(travel) > 1.0:
            duration_ms = self.calibration.chassis_duration_ms(travel, self.speed_pct)
            if duration_ms < self.calibration.value("limits", "min_action_ms"):
                note["skipped"] = "dolly below min_action_ms"
            else:
                direction = self.calibration.value("chassis", "dir_close") if travel < 0 \
                    else self.calibration.value("chassis", "dir_far")
                add("chassis_drive", {"dir": direction, "speed": self.speed_pct, "duration_ms": duration_ms})
                note["dolly_seconds"] = round(duration_ms / 1000.0, 2)
                note["segment_seconds"] = round(right.time_offset - left.time_offset, 2)
                note["timing_ok"] = note["dolly_seconds"] <= note["segment_seconds"] + 1e-9
        self.notes.append(note)
        return actions

    def compile(self, trajectory, context, registry, reachability) -> ShotExecutionPlan:
        trajectory = TargetTrajectory.model_validate(trajectory.model_dump())
        context = AgentState.model_validate(context.model_dump())
        registry = ActionRegistry.model_validate(registry.model_dump())
        if context.status != "PLANNED":
            raise AgentError("INVALID_STATE_TRANSITION", "Compilation requires a PLANNED context")
        if context.registry_revision != registry.revision:
            raise AgentError("REGISTRY_MISMATCH", "Compiler context does not match registry")
        require_reachable(trajectory, reachability)
        self.notes = []
        actions = []
        for index, (left, right) in enumerate(zip(trajectory.keyframes, trajectory.keyframes[1:])):
            actions.extend(self._segment_actions(left, right, context, registry, index))
        if not actions:
            # Nothing to move: an explicit hold keeps the plan contract non-empty.
            actions.append(StructuredAction(action_id=f"{context.shot_id}-hold-{context.plan_id[:6]}",
                                            shot_id=context.shot_id, plan_id=context.plan_id, source="INITIAL",
                                            action_name="actuator_stop", parameters={"target": "all", "emergency": False},
                                            registry_revision=registry.revision))
        return validate_plan(ShotExecutionPlan(plan_id=context.plan_id, shot_id=context.shot_id,
                                               registry_revision=registry.revision, actions=actions,
                                               execution_relation="SEQUENTIAL"), registry)
