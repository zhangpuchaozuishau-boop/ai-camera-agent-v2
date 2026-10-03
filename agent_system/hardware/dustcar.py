"""DustCar ESP32-S3 HTTP client (App API spec 2026-10-03).

Stdlib only. Every method raises AgentError with a stable code:
  DEVICE_UNREACHABLE  transport/HTTP failure (robot off, wrong WiFi, timeout)
  DEVICE_REJECTED     firmware answered {"ok": false, "error": ...}
  INVALID_PARAMETERS  caller sent values the firmware will refuse (caught locally)
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import AgentError

DIRECTIONS = ("fwd", "back", "left", "right", "rot_l", "rot_r", "stop")
STOP_TARGETS = ("all", "motors", "motor1", "motor2", "servos")


class DustCarClient:
    """One HTTP connection per request; firmware has no auth and no keep-alive state."""

    def __init__(self, host="192.168.4.1", *, port=80, timeout=2.0, opener=None):
        self.host, self.port, self.timeout = host, port, timeout
        self._opener = opener or urllib.request.urlopen
        self.base_url = f"http://{host}" + ("" if port == 80 else f":{port}")
        self.requests = []  # (method, path, payload, response) for the debug console

    # -- transport ---------------------------------------------------------
    def _call(self, method, path, *, params=None, body=None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            self.requests.append((method, path, {"params": params, "body": body},
                                  {"http_error": exc.code, "body": raw}))
            detail = raw[:200]
            try:  # the documented failure envelope carries the firmware's own message
                parsed = json.loads(raw)
                detail = str(parsed.get("error") or parsed or detail)
            except ValueError:
                pass
            raise AgentError("DEVICE_REJECTED", f"{path} returned HTTP {exc.code}: {detail}",
                             {"path": path, "http_status": exc.code, "body": raw[:200]}) from exc
        except Exception as exc:  # URLError, timeout, socket errors
            self.requests.append((method, path, {"params": params, "body": body},
                                  {"transport_error": type(exc).__name__}))
            raise AgentError("DEVICE_UNREACHABLE", f"{path} transport failed",
                             {"path": path, "exception_type": type(exc).__name__}) from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            self.requests.append((method, path, {"params": params, "body": body}, {"malformed": raw[:120]}))
            raise AgentError("DEVICE_REJECTED", f"{path} returned non-JSON", {"body": raw[:200]}) from exc
        self.requests.append((method, path, {"params": params, "body": body}, payload))
        if payload.get("ok") is False:
            raise AgentError("DEVICE_REJECTED", str(payload.get("error", "unspecified firmware error")),
                             {"path": path, "body": payload})
        return payload

    # -- E1/E2 chassis -----------------------------------------------------
    def chassis(self, direction, speed=60):
        if direction not in DIRECTIONS:
            raise AgentError("INVALID_PARAMETERS", "Unknown chassis direction",
                             {"dir": direction, "allowed": list(DIRECTIONS)})
        if not isinstance(speed, int) or not 10 <= speed <= 100:
            raise AgentError("INVALID_PARAMETERS", "speed must be an int in 10..100", {"speed": speed})
        return self._call("GET", "/api/cmd", params={"dir": direction, "speed": speed})

    def chassis_stop(self):
        return self._call("GET", "/api/cmd", params={"dir": "stop"})

    def status(self):
        return self._call("GET", "/api/status")

    # -- E3/E4/E5/E6 actuator ---------------------------------------------
    def motor(self, motor_id, direction, rpm, duration_ms=0):
        if motor_id not in (1, 2):
            raise AgentError("INVALID_PARAMETERS", "motor id must be 1 or 2", {"id": motor_id})
        if direction not in (0, 1):
            raise AgentError("INVALID_PARAMETERS", "motor dir must be 0 or 1", {"dir": direction})
        if not isinstance(rpm, int) or not 1 <= rpm <= 3000:
            raise AgentError("INVALID_PARAMETERS", "rpm must be an int in 1..3000", {"rpm": rpm})
        if not isinstance(duration_ms, int) or duration_ms < 0:
            raise AgentError("INVALID_PARAMETERS", "duration_ms must be a nonnegative int",
                             {"duration_ms": duration_ms})
        return self._call("POST", "/api/motor",
                          body={"id": motor_id, "dir": direction, "rpm": rpm, "duration_ms": duration_ms})

    def servo(self, servo_id, angle):
        if servo_id not in (1, 2):
            raise AgentError("INVALID_PARAMETERS", "servo id must be 1 or 2", {"id": servo_id})
        if not isinstance(angle, int) or not 0 <= angle <= 180:
            raise AgentError("INVALID_PARAMETERS", "servo angle must be an int in 0..180", {"angle": angle})
        return self._call("POST", "/api/servo", body={"id": servo_id, "angle": angle})

    def stop(self, target="all", emergency=False):
        if target not in STOP_TARGETS:
            raise AgentError("INVALID_PARAMETERS", "Unknown stop target",
                             {"target": target, "allowed": list(STOP_TARGETS)})
        return self._call("POST", "/api/stop", body={"target": target, "emergency": bool(emergency)})

    def actuator(self):
        return self._call("GET", "/api/actuator")

    # -- E7 reserved -------------------------------------------------------
    def store_target(self, x, y, yaw):
        return self._call("POST", "/api/target", body={"x": x, "y": y, "yaw": yaw})

    def emergency_stop(self):
        """Firmware requires BOTH calls: /api/stop does not touch the mecanum wheels."""
        actuator = self.stop("all", emergency=True)
        chassis = self.chassis_stop()
        return {"actuator": actuator, "chassis": chassis}


class ChassisKeepalive:
    """The firmware auto-stops after ~1200 ms without a new /api/cmd.

    Holding a move therefore means re-sending the same dir every interval_ms.
    """

    def __init__(self, client, direction, speed=60, *, interval_ms=180):
        self.client, self.direction, self.speed = client, direction, speed
        self.interval = interval_ms / 1000.0
        self.sent = 0
        self.errors = []
        self._stop = threading.Event()
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.client.chassis(self.direction, self.speed)
                self.sent += 1
            except AgentError as error:
                self.errors.append({"code": error.code, "reason": error.reason})
                self.stop()  # never keep commanding an unreachable robot
                return
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is not None:
            raise AgentError("INVALID_STATE_TRANSITION", "Keepalive already started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self, *, send_stop=True, timeout=1.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if send_stop:
            try:
                self.client.chassis_stop()
            except AgentError as error:
                self.errors.append({"code": error.code, "reason": error.reason})
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def hold_chassis(client, direction, seconds, *, speed=60, interval_ms=180, sleep=time.sleep):
    """Drive one direction for a fixed time, keeping the watchdog fed, then stop."""
    driver = ChassisKeepalive(client, direction, speed, interval_ms=interval_ms).start()
    try:
        sleep(seconds)
    finally:
        driver.stop()
    return {"dir": direction, "seconds": seconds, "keepalives": driver.sent}
