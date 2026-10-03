"""End-to-end hardware check: console -> real HTTP -> fake firmware.

    python -m webui.fake_firmware --port 8899     # terminal 1
    python -m webui.server --port 8765            # terminal 2
    python webui/hw_e2e_check.py                  # this file

Proves the agent drives the documented firmware contract without touching the robot.
"""

import json
import time
import urllib.error
import urllib.request

CONSOLE = "http://127.0.0.1:8765/"
HOST = "127.0.0.1:8899"  # fake firmware; use 192.168.4.1 for the real rig


def post(name, body):
    req = urllib.request.Request(CONSOLE + "api/" + name, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def get(path):
    return json.loads(urllib.request.urlopen(CONSOLE + path.lstrip("/"), timeout=30).read())


if __name__ == "__main__":
    config = get("api/hw/config")
    print(f"registry {config['registry_revision']} · 未标定常量 {len(config['unverified'])} 个 · 默认 {config['default_host']}")

    print("连通性:", post("hw/poll", {"host": HOST})["ok"])

    board = post("storyboard", {"text": "青铜器展示", "mode": "fixture"})
    script = board["script"]
    shot_id = script["shots"][0]["shot_id"]
    print(f"分镜（离线）: {script['shots'][0]['shot_goal']}")

    plan = post("hw/plan", {"script": script, "shot_id": shot_id, "host": HOST,
                            "subject_height_mm": 200, "speed_pct": 40})
    print(f"\n[只算不发] 动作 {len(plan['actions'])} 个 · 累计 {plan['commanded_seconds']}s（上限 {plan['max_run_seconds']}s）")
    for action in plan["actions"]:
        print("   ", action["action_name"], json.dumps(action["parameters"], ensure_ascii=False))
    for note in plan["notes"]:
        print(f"    段{note['segment']} {note['t0']}-{note['t1']}s h{note['height_ratio']} "
              f"D{note['distance_mm']}mm 进退{note['dolly_mm']}mm 角{note['pan_deg']}°/{note['tilt_deg']}° "
              f"耗时{note.get('dolly_seconds')}s/段{note.get('segment_seconds')}s 及时={note.get('timing_ok')}")

    refused = post("hw/run", {"script": script, "shot_id": shot_id, "host": HOST,
                              "subject_height_mm": 200, "speed_pct": 40})
    print("\n无 confirm 的下发请求:", refused["error"]["code"])

    run = post("hw/run", {"script": script, "shot_id": shot_id, "host": HOST, "confirm": True,
                          "subject_height_mm": 200, "speed_pct": 40})
    print("下发:", run["plan_id"], run["host"], [e["status"] for e in run["events"]])
    state = None
    for _ in range(60):
        time.sleep(0.5)
        state = post("hw/state", {"plan_id": run["plan_id"]})
        if not state["is_running"]:
            break
    print("事件:", [e["status"] for e in state["events"]], "错误:", state["error"])
    for entry in state["action_log"]:
        print(f"   {entry['action_name']:<14} ok={entry['ok']} {json.dumps(entry['parameters'], ensure_ascii=False)} "
              f"{entry.get('response') if entry['action_name'] != 'chassis_drive' else ''} {entry['seconds']}s")
    print("HTTP 请求共", len(state["requests"]), "条；示例:")
    for row in state["requests"][:3] + state["requests"][-2:]:
        print("   ", row["method"], row["path"], json.dumps(row["payload"], ensure_ascii=False)[:110])
    kinds = [r["path"] for r in state["requests"]]
    print("请求分布:", {k: kinds.count(k) for k in sorted(set(kinds))})

    print("\n急停:", post("hw/stop", {"host": HOST})["result"])
    recorded = urllib.request.urlopen("http://" + HOST + "/_requests", timeout=10).read().decode()
    print("假固件记录的请求数:", json.loads(recorded)["count"])
