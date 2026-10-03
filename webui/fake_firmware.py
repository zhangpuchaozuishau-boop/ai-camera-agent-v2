"""Standalone fake DustCar firmware, for console rehearsals without the robot.

    .venv/Scripts/python.exe -m webui.fake_firmware --port 8899

Implements the documented endpoints and records every request to
webui/runs/fake_firmware_requests.jsonl so you can diff what the agent sent
against the spec. It never moves anything. Change nothing here to test the real
robot: point the console at 192.168.4.1 instead (and read the notes first).
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

RUNS = Path(__file__).resolve().parent / "runs"
STATE = {"motors": {1: {"running": False, "dir": 0, "rpm": 0, "remain_ms": 0, "until": 0.0},
                    2: {"running": False, "dir": 0, "rpm": 0, "remain_ms": 0, "until": 0.0}},
         "servos": {1: 90, 2: 90}, "mode": "idle", "speed": 0.0}


class FakeFirmware(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[fake-fw] " + fmt % args)

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        if parsed.path == "/_requests":  # inspection helper, not part of the spec
            return self._reply(200, {"count": len(self.server.requests), "requests": self.server.requests[-40:]})
        RUNS.mkdir(exist_ok=True)
        with open(RUNS / "fake_firmware_requests.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.strftime("%H:%M:%S"), "method": self.command, "path": parsed.path,
                                     "query": query, "body": body}, ensure_ascii=False) + "\n")
        self.server.requests.append({"method": self.command, "path": parsed.path, "query": query, "body": body})

        if parsed.path == "/api/cmd":
            direction = query.get("dir", "stop")
            if direction == "stop" or direction not in ("fwd", "back", "left", "right", "rot_l", "rot_r"):
                STATE.update(mode="idle", speed=0.0)
            else:
                STATE.update(mode="manual", speed=float(query.get("speed", 60)) / 100.0)
            return self._reply(200, {"ok": True})
        if parsed.path == "/api/status":
            return self._reply(200, {"mode": STATE["mode"], "battery_mv": 7400, "battery_ok": True,
                                     "speed": STATE["speed"], "meas": [0.0] * 4, "target": [0.0] * 4, "pwm": [0] * 4,
                                     "pose_target": {"x": 0.0, "y": 0.0, "yaw": 0.0, "stored": False}})
        if parsed.path == "/api/motor":
            if not isinstance(body, dict) or not all(k in body for k in ("id", "dir", "rpm")):
                return self._reply(400, {"ok": False, "error": "need id,dir,rpm"})
            if body["id"] not in (1, 2):
                return self._reply(400, {"ok": False, "error": "id must be 1 or 2"})
            if not 1 <= int(body["rpm"]) <= 3000:
                return self._reply(400, {"ok": False, "error": "rpm must be 1-3000"})
            duration = int(body.get("duration_ms", 0))
            STATE["motors"][body["id"]] = {"running": True, "dir": int(body["dir"]), "rpm": int(body["rpm"]),
                                           "remain_ms": duration, "until": time.monotonic() + duration / 1000.0}
            return self._reply(200, {"ok": True})
        if parsed.path == "/api/servo":
            if not isinstance(body, dict) or "id" not in body or "angle" not in body:
                return self._reply(400, {"ok": False, "error": "need id,angle"})
            if body["id"] not in (1, 2) or not 0 <= int(body["angle"]) <= 180:
                return self._reply(400, {"ok": False, "error": "bad servo target"})
            STATE["servos"][body["id"]] = int(body["angle"])
            return self._reply(200, {"ok": True})
        if parsed.path == "/api/stop":
            target = (body or {}).get("target", "all")
            emergency = bool((body or {}).get("emergency", False))
            for index in (1, 2):
                if target in ("all", "motors", f"motor{index}"):
                    STATE["motors"][index].update(running=False, remain_ms=0, until=0.0)
                if target in ("all", "servos"):
                    STATE["servos"][index] = 90
            return self._reply(200, {"ok": True, "emergency": emergency})
        if parsed.path == "/api/actuator":
            now = time.monotonic()
            motors = []
            for index in (1, 2):
                motor = STATE["motors"][index]
                remain = max(0, int((motor["until"] - now) * 1000)) if motor["running"] else 0
                motors.append({"id": index, "running": motor["running"] and remain > 0, "dir": motor["dir"],
                               "rpm": motor["rpm"], "remain_ms": remain})
            return self._reply(200, {"ok": True, "ip": self.server.server_address[0], "motors": motors,
                                     "servos": [{"id": i, "angle": STATE["servos"][i]} for i in (1, 2)]})
        if parsed.path == "/api/target":
            return self._reply(200, {"ok": True, "stored": True})
        return self._reply(404, {"ok": False, "error": "unknown route"})

    do_GET = do_POST = _handle
    do_OPTIONS = lambda self: self._reply(200, {"ok": True})  # noqa: E731 (spec section 2.2)


def main():
    parser = argparse.ArgumentParser(description="Fake DustCar firmware for console rehearsals")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), FakeFirmware)
    server.requests = []
    print(f"Fake DustCar firmware on http://{args.host}:{args.port}  (requests logged to webui/runs/)")
    server.serve_forever()


if __name__ == "__main__":
    main()
