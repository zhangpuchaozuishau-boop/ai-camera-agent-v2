"""Visual correction intent -> DustCar actuator commands (the closed loop's second half).

Feedback V2 stops at a TrajectoryCorrectionIntent in VISUAL dimensions. This module
turns one into real commands, using the SAME geometry as compiler.py:

    CENTER_X              -> pan servo delta   dpan  = pan_deg(target_cx) - pan_deg(observed_cx)
    CENTER_Y              -> tilt servo delta  dtilt = tilt_deg(target_cy) - tilt_deg(observed_cy)
    SUBJECT_HEIGHT_RATIO  -> dolly              dD    = D(target_h) - D(observed_h)
    DISTANCE              -> dolly              dD    = target - observed (canonical units)

Sign conventions are inheritance from the camera model: increasing pan moves the
subject LEFT in the image (u = cx0 - fx*tan(pan)), increasing tilt moves it DOWN
(v = cy0 + fy*tan(tilt)). Both are flipped by the servo `sign` in calibration.json.

Corrections are bounded by limits.max_correction_mm / max_correction_deg: a bigger
error is clamped and reported, never executed at full size. No component that can be
mapped means no motion: the caller pauses and asks a human.
"""

import math
from decimal import Decimal
from uuid import uuid4

from ..errors import AgentError
from ..models import ContractModel, StructuredAction, TrajectoryCorrectionIntent
from .compiler import DustCarCalibration


class CorrectionBudget(ContractModel):
    max_correction_mm: float
    max_correction_deg: float
    min_interval_s: float


def budget_from(calibration: DustCarCalibration) -> CorrectionBudget:
    limits = calibration.data["limits"]
    return CorrectionBudget(max_correction_mm=limits["max_correction_mm"]["value"],
                            max_correction_deg=limits["max_correction_deg"]["value"],
                            min_interval_s=limits["min_correction_interval_s"]["value"])


class CorrectionCompiler:
    """Stateful: it needs the last COMMANDED servo angles to emit absolute angles.

    E6 (/api/actuator) reports the last commanded angle, so the loop can resync if
    another client moved the rig. No encoder is involved.
    """

    def __init__(self, calibration: DustCarCalibration | None = None, *, servo_angles=None,
                 subject_height_mm=None, speed_pct=None):
        self.calibration = calibration or DustCarCalibration.load()
        self.subject_height_mm = subject_height_mm
        self.speed_pct = int(speed_pct or self.calibration.value("chassis", "default_speed_pct"))
        self.budget = budget_from(self.calibration)
        self.servo_angles = dict(servo_angles or {"pan": self.calibration.data["servos"]["pan"]["zero_angle"],
                                                 "tilt": self.calibration.data["servos"]["tilt"]["zero_angle"]})
        self.notes = []

    def sync_servo_angles(self, actuator_status: dict):
        """Resync from E6: servos[].angle is the firmware's last commanded angle."""
        by_id = {int(s["id"]): int(s["angle"]) for s in actuator_status.get("servos", [])}
        for joint in ("pan", "tilt"):
            servo_id = self.calibration.data["servos"][joint]["id"]
            if servo_id in by_id:
                self.servo_angles[joint] = by_id[servo_id]
        return self.servo_angles

    def _servo_action(self, joint, delta_deg, intent, index, registry, *, requested_deg=None, clamped=False):
        spec = self.calibration.data["servos"][joint]
        requested = self.servo_angles[joint] + spec["sign"] * delta_deg * spec["deg_per_unit"]
        limits = self.calibration.data["servos"]
        bounded = min(limits["safe_max_angle"]["value"], max(limits["safe_min_angle"]["value"], requested))
        self.servo_angles[joint] = int(round(bounded))
        note = {"dimension": joint.upper(), "delta_deg": round(delta_deg, 2),
                "requested_delta_deg": None if requested_deg is None else round(requested_deg, 2),
                "requested_angle": round(requested, 2), "commanded_angle": self.servo_angles[joint],
                "clamped": bool(clamped) or abs(requested - bounded) > 1e-9}
        return note, StructuredAction(action_id=f"corr-{uuid4().hex[:8]}-{index}", shot_id=intent.shot_id,
                                      plan_id=intent.plan_id, source="CORRECTION", action_name="servo_set",
                                      parameters={"id": spec["id"], "angle": self.servo_angles[joint]},
                                      registry_revision=registry.revision)

    def compile(self, intent, registry):
        """Returns (actions, notes). Raises UNSUPPORTED_CORRECTION for unmappable input."""
        intent = TrajectoryCorrectionIntent.model_validate(intent.model_dump())
        actions, notes = [], []
        for index, component in enumerate(intent.components):
            dimension, target, observed = component.dimension, component.target_value, component.observed_value
            if dimension in ("CENTER_X", "CENTER_Y"):
                joint = "pan" if dimension == "CENTER_X" else "tilt"
                angle_of = self.calibration.pan_deg if dimension == "CENTER_X" else self.calibration.tilt_deg
                requested = angle_of(target) - angle_of(observed)
                delta = requested
                clamped = False
                if abs(delta) > self.budget.max_correction_deg:
                    delta = math.copysign(self.budget.max_correction_deg, delta)
                    clamped = True
                if abs(delta) < 0.05:
                    continue
                note, action = self._servo_action(joint, delta, intent, index, registry,
                                                  requested_deg=requested, clamped=clamped)
                notes.append(note), actions.append(action)
            elif dimension == "SUBJECT_HEIGHT_RATIO":
                travel = self.calibration.distance_mm(target, self.subject_height_mm) - \
                    self.calibration.distance_mm(observed, self.subject_height_mm)
                note, action = self._dolly_action(travel, intent, index, registry, dimension)
                notes.append(note), actions.append(action)
            elif dimension == "DISTANCE":
                note, action = self._dolly_action(target - observed, intent, index, registry, dimension)
                notes.append(note), actions.append(action)
            else:
                raise AgentError("UNSUPPORTED_CORRECTION", "Correction dimension cannot be mapped to this rig",
                                 {"dimension": dimension})
        if not actions:
            raise AgentError("UNSUPPORTED_CORRECTION", "Correction is inside the actuator's resolution",
                             {"components": [c.dimension for c in intent.components]})
        return actions, notes

    def _dolly_action(self, travel_mm, intent, index, registry, dimension):
        clamped = max(-self.budget.max_correction_mm, min(self.budget.max_correction_mm, travel_mm))
        duration_ms = self.calibration.chassis_duration_ms(clamped, self.speed_pct)
        if duration_ms < self.calibration.value("limits", "min_action_ms"):
            raise AgentError("UNSUPPORTED_CORRECTION", "Correction is below the minimum action time",
                             {"travel_mm": round(clamped, 2), "duration_ms": duration_ms})
        direction = self.calibration.value("chassis", "dir_close") if clamped < 0 \
            else self.calibration.value("chassis", "dir_far")
        note = {"dimension": dimension, "travel_mm": round(clamped, 1), "requested_mm": round(travel_mm, 1),
                "clamped": abs(clamped - travel_mm) > 1e-6, "duration_ms": duration_ms, "dir": direction,
                "speed_pct": self.speed_pct}
        action = StructuredAction(action_id=f"corr-{uuid4().hex[:8]}-{index}", shot_id=intent.shot_id,
                                  plan_id=intent.plan_id, source="CORRECTION", action_name="chassis_drive",
                                  parameters={"dir": direction, "speed": self.speed_pct, "duration_ms": duration_ms},
                                  registry_revision=registry.revision)
        return note, action


def predicted_gap(calibration: DustCarCalibration, component) -> dict:
    """Review helper: what the correction would move, without touching the rig."""
    if component.dimension == "CENTER_X":
        deg = calibration.pan_deg(component.target_value) - calibration.pan_deg(component.observed_value)
        return {"joint": "pan", "delta_deg": round(deg, 2)}
    if component.dimension == "CENTER_Y":
        deg = calibration.tilt_deg(component.target_value) - calibration.tilt_deg(component.observed_value)
        return {"joint": "tilt", "delta_deg": round(deg, 2)}
    d_target = calibration.distance_mm(component.target_value)
    d_observed = calibration.distance_mm(component.observed_value)
    return {"joint": "chassis", "travel_mm": round(Decimal(str(d_target)) - Decimal(str(d_observed)), 1)}
