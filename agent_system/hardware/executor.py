"""Executor adapter for the DustCar firmware.

Runs a validated ShotExecutionPlan against the real HTTP API, sequentially,
emitting the ExecutionEvent lifecycle the state machine already consumes:
    accepted -> running -> completed | failed

Safety properties this adapter is responsible for, because the firmware has none:
  * the chassis is re-commanded every ~180ms (firmware stops it after 1200ms)
  * nothing keeps running after PAUSE / lost target / e-stop
  * every action's HTTP outcome is recorded, including failures

PAUSE is a proposal until confirm_pause(): abort() performs the physical stop.
"""

import threading
import time
from decimal import Decimal

from ..errors import AgentError
from ..models import ExecutionEvent
from ..registry import ActionRegistry
from ..validation import validate_action, validate_plan


class DryRunClient:
    """Same surface as DustCarClient, records instead of transmitting.

    Values are NOT firmware-validated here; the real client does that.
    """

    def __init__(self):
        self.requests = []
        self.base_url = "dry-run://no-transmit"

    def _record(self, method, path, payload=None):
        self.requests.append((method, path, payload, {"ok": True, "dry_run": True}))
        return {"ok": True}

    def chassis(self, direction, speed=60):
        return self._record("GET", "/api/cmd", {"dir": direction, "speed": speed})

    def chassis_stop(self):
        return self._record("GET", "/api/cmd", {"dir": "stop"})

    def status(self):
        return {"mode": "idle", "battery_mv": 0, "battery_ok": True, "speed": 0.0, "meas": [0.0] * 4,
                "target": [0.0] * 4, "pwm": [0] * 4, "pose_target": {"x": 0, "y": 0, "yaw": 0, "stored": False},
                "dry_run": True}

    def motor(self, motor_id, direction, rpm, duration_ms=0):
        return self._record("POST", "/api/motor",
                            {"id": motor_id, "dir": direction, "rpm": rpm, "duration_ms": duration_ms})

    def servo(self, servo_id, angle):
        return self._record("POST", "/api/servo", {"id": servo_id, "angle": angle})

    def stop(self, target="all", emergency=False):
        return self._record("POST", "/api/stop", {"target": target, "emergency": bool(emergency)})

    def actuator(self):
        return {"ok": True, "ip": self.base_url, "motors": [{"id": 1, "running": False, "dir": 0, "rpm": 0, "remain_ms": 0},
                                                            {"id": 2, "running": False, "dir": 0, "rpm": 0, "remain_ms": 0}],
                "servos": [{"id": 1, "angle": 90}, {"id": 2, "angle": 90}], "dry_run": True}

    def store_target(self, x, y, yaw):
        return self._record("POST", "/api/target", {"x": x, "y": y, "z": yaw})

    def emergency_stop(self):
        return {"actuator": self.stop("all", emergency=True), "chassis": self.chassis_stop()}


class DustCarExecutor:
    """Executes a plan on the rig. One plan at a time."""

    def __init__(self, client, registry: ActionRegistry, *, keepalive_ms=None, time_scale=1.0,
                 sleep=time.sleep, clock=time.monotonic, max_runtime_s=600.0):
        self.client = client
        self.registry = ActionRegistry.model_validate(registry.model_dump())
        self.keepalive_ms = keepalive_ms
        self.time_scale = time_scale
        self._sleep = sleep
        self._clock = clock
        self.max_runtime_s = max_runtime_s
        self._events = []
        self.action_log = []
        self._plan = None
        self._abort = threading.Event()
        self._thread = None
        self.error = None

    # -- event plumbing ----------------------------------------------------
    @property
    def events(self):
        return list(self._events)

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _record(self, status, reason, action_id=None):
        event = ExecutionEvent(plan_id=self._plan.plan_id, action_id=action_id, status=status, reason=reason[:120])
        self._events.append(event)
        return event

    # -- lifecycle ---------------------------------------------------------
    def submit_plan(self, plan):
        validated = validate_plan(plan, self.registry)
        self._plan = validated
        self._abort.clear()
        self.action_log = []
        self.error = None
        return self._record("accepted", "Adapter validated the submission")

    def start(self, plan_id, *, action_id=None):
        if self._plan is None or self._plan.plan_id != plan_id:
            raise AgentError("INVALID_STATE_TRANSITION", "No submitted plan matches this plan_id")
        if self.is_running:
            raise AgentError("INVALID_STATE_TRANSITION", "This plan is already executing")
        self._thread = threading.Thread(target=self._execute, daemon=True)
        self._thread.start()
        return self._record("running", "Execution started by the adapter")

    def execute_corrections(self, actions, *, registry=None):
        """Run CORRECTION actions outside a plan. validate_action enforces that the
        registry permits them for correction; failures stop everything, like a plan."""
        registry = registry or self.registry
        log = []
        try:
            for action in actions:
                validated = validate_action(action, registry)
                log.append(self._run_action(validated))
        except AgentError as error:
            self.error = error
            try:
                self.client.emergency_stop()
            except AgentError:
                pass
            log.append({"ok": False, "error": {"code": error.code, "reason": error.reason}})
        self.action_log.extend(log)
        return log

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return self.events

    def run_blocking(self, plan):
        """Synchronous path used by tests: submit (already validated) then execute inline."""
        self._plan = plan
        self._abort.clear()
        self.action_log = []
        self.error = None
        self._execute()
        return self.events

    # -- execution ---------------------------------------------------------
    def _execute(self):
        started = self._clock()
        try:
            for action in self._plan.actions:
                if self._abort.is_set():
                    raise AgentError("EXECUTION_ABORTED", "Aborted before the next action")
                if self._clock() - started > self.max_runtime_s:
                    raise AgentError("EXECUTION_TIMEOUT", "Plan exceeded the runtime guard")
                entry = self._run_action(action)
                self.action_log.append(entry)
        except AgentError as error:
            self.error = error
            try:
                self.client.emergency_stop()
                stopped = "adapter stopped chassis and actuators"
            except AgentError as stop_error:
                stopped = f"stop also failed: {stop_error.code}"
            self._record("failed", f"{error.code}: {error.reason} ({stopped})")
            return
        self._record("completed", "All actions acknowledged by the firmware")

    def _run_action(self, action):
        name, parameters = action.action_name, action.parameters
        started = self._clock()
        response = None
        try:
            if name == "servo_set":
                response = self.client.servo(parameters["id"], parameters["angle"])
            elif name == "chassis_drive":
                response = self._drive(parameters)
            elif name == "chassis_stop":
                response = self.client.chassis_stop()
            elif name == "stepper_run":
                response = self.client.motor(parameters["id"], parameters["dir"], parameters["rpm"],
                                             parameters["duration_ms"])
                if parameters["duration_ms"] > 0:
                    self._wait(parameters["duration_ms"] / 1000.0)
            elif name == "actuator_stop":
                response = self.client.stop(parameters["target"], parameters["emergency"])
            else:
                raise AgentError("UNKNOWN_ACTION", f"Adapter cannot execute {name!r}")
        except AgentError as error:
            self.action_log.append({"action_id": action.action_id, "action_name": name, "parameters": parameters,
                                    "ok": False, "error": {"code": error.code, "reason": error.reason},
                                    "seconds": round(self._clock() - started, 3)})
            raise
        return {"action_id": action.action_id, "action_name": name, "parameters": parameters, "ok": True,
                "response": response, "seconds": round(self._clock() - started, 3)}

    def _drive(self, parameters):
        """Keep the firmware watchdog fed for the requested duration, then stop."""
        from .dustcar import ChassisKeepalive

        hold = parameters["duration_ms"] / 1000.0 * self.time_scale
        interval_ms = max(1, int((self.keepalive_ms or 180) * self.time_scale))
        driver = ChassisKeepalive(self.client, parameters["dir"], parameters["speed"],
                                  interval_ms=interval_ms).start()
        try:
            self._wait(hold)
        finally:
            driver.stop()
        if driver.errors:
            raise AgentError(driver.errors[0]["code"], driver.errors[0]["reason"])
        return {"dir": parameters["dir"], "speed": parameters["speed"], "keepalives": driver.sent,
                "seconds": round(hold, 2)}

    def _wait(self, seconds):
        """Interruptible wait: abort() returns control immediately."""
        deadline = self._clock() + max(0.0, seconds)
        while True:
            if self._abort.is_set():
                raise AgentError("EXECUTION_ABORTED", "Stopped during an action")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._sleep(min(0.05, remaining))

    # -- operator controls -------------------------------------------------
    def abort(self, *, emergency=False, reason="Operator abort"):
        """Physical stop: /api/stop (actuator) AND dir=stop (chassis)."""
        self._abort.set()
        result = {"reason": reason}
        try:
            if emergency:
                result["stop"] = self.client.emergency_stop()
            else:
                result["chassis"] = self.client.chassis_stop()
                result["actuator"] = self.client.stop("all", False)
        except AgentError as error:
            result["error"] = {"code": error.code, "reason": error.reason}
        if self.is_running:
            self._thread.join(2.0)
        return result

    def poll(self):
        """Both status routes, kept separate on purpose (spec section 3)."""
        out = {}
        for key, call in (("chassis", self.client.status), ("actuator", self.client.actuator)):
            try:
                out[key] = call()
            except AgentError as error:
                out[key] = {"error": {"code": error.code, "reason": error.reason}}
        return out
