"""Simulate the phone's connection attempt with the app's own budget.

Mirrors what the Android app sends and measures the reply against its timeouts
(connect 1.5s / read 1.5s / total 3s):

  POST /api/app/hello
  Content-Type: application/json; charset=utf-8
  Accept: application/json
  X-Agent-Token: <only when the agent was started with --token>
  {"host": "192.168.4.1", "probe": true}

  .venv/Scripts/python.exe webui/app_hello_probe.py [--base http://172.20.10.2:8765] [--robot 192.168.4.1]
                                                     [--token x] [--no-probe] [--repeat 3]
"""

import argparse
import json
import socket
import time
import urllib.error
import urllib.request

APP_CONNECT_TIMEOUT = 1.5      # what the app allows to connect
APP_READ_TIMEOUT = 1.5         # what the app allows to read
APP_TOTAL_BUDGET = 3.0         # what the app allows in total


def attempt(base, host, token, probe, label):
    body = json.dumps({"host": host, "probe": probe}).encode()
    request = urllib.request.Request(base.rstrip("/") + "/api/app/hello", data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "AgentApp/0.6.0 (Android; connection-probe)")
    if token:
        request.add_header("X-Agent-Token", token)
    started = time.perf_counter()
    verdict, payload, error = "OK", None, None
    try:
        with urllib.request.urlopen(request, timeout=APP_READ_TIMEOUT) as response:
            raw = response.read()
        elapsed = time.perf_counter() - started
        payload = json.loads(raw)
    except socket.timeout:
        elapsed = time.perf_counter() - started
        verdict, error = "TIMEOUT（读超时）", f"超过 App 的 {APP_READ_TIMEOUT}s 预算"
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - started
        verdict, error = f"HTTP {exc.code}", exc.read()[:200].decode(errors="replace")
    except OSError as exc:
        elapsed = time.perf_counter() - started
        verdict, error = "连不上", str(exc)
    budget = "在预算内" if elapsed <= APP_TOTAL_BUDGET else "超出 3s 总预算"
    print(f"{label}: {verdict}　耗时 {elapsed * 1000:.0f}ms（{budget}）")
    if error:
        print(f"    错误：{error}")
    if payload:
        robot = payload.get("robot", {})
        print(f"    api={payload.get('api_version')}　标定 {payload['calibration']['measured']}/{payload['calibration']['total']}"
              f"　机器人 {robot.get('state')} reachable={robot.get('reachable')}"
              f"　hello 内部耗时 {payload.get('timings', {}).get('total_ms')}ms")
        if robot.get("status"):
            print(f"    机器人状态：{json.dumps(robot['status'], ensure_ascii=False)[:160]}")
        if robot.get("hint"):
            print(f"    提示：{robot['hint']}")
    return elapsed, verdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--robot", default="192.168.4.1")
    parser.add_argument("--token", default="")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    print(f"目标 {args.base}/api/app/hello　机器人 {args.robot}　"
          f"App 预算：连接 {APP_CONNECT_TIMEOUT}s / 读 {APP_READ_TIMEOUT}s / 总 {APP_TOTAL_BUDGET}s\n")
    probe = not args.no_probe
    for index in range(args.repeat):
        attempt(args.base, args.robot, args.token, probe,
                f"[{index + 1}/{args.repeat}] probe={probe}")
        if index == 0 and probe:
            print("    （第一次可能落在后台探测窗口内，第二次应命中缓存）")
        time.sleep(0.3)
    print("\n不带机器人探测的对照：")
    attempt(args.base, args.robot, args.token, False, "[对照] probe=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
