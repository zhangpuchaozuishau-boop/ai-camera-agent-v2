"""App-side self check: walks the exact sequence the phone app will use.

  .venv/Scripts/python.exe -m webui.fake_firmware --port 8899
  .venv/Scripts/python.exe -m webui.server --port 8765
  .venv/Scripts/python.exe webui/app_e2e_check.py [--agent http://127.0.0.1:8765] [--robot 127.0.0.1:8899]

Storyboard comes from the offline fixture (no LLM cost); every robot command goes
over real HTTP to whatever address you pass as --robot (fake firmware by default).
"""

import argparse
import json
import time
import urllib.error
import urllib.request


def post(base, path, body):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def show(label, payload, keys=None):
    if keys:
        payload = {key: payload.get(key) for key in keys}
    print(f"  {label}: " + json.dumps(payload, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="http://127.0.0.1:8765")
    parser.add_argument("--robot", default="127.0.0.1:8899")
    args = parser.parse_args()
    base = args.agent.rstrip("/") + "/api/"

    print("1) 握手 /api/app/hello")
    status, hello = post(base, "app/hello", {"host": args.robot, "probe": True})
    print(f"   HTTP {status}  api {hello.get('api_version')}  机器人可达={hello['robot']['reachable']} "
          f"电量={ (hello['robot'].get('status') or {}).get('battery_mv') }mV")
    print(f"   标定 {hello['calibration']['measured']}/{hello['calibration']['total']} 已标定；"
          f"未标定项 {len(hello['calibration']['missing'])} 个（距离/角度暂不可信）")
    print(f"   安全上限 {json.dumps(hello['capabilities']['limits'], ensure_ascii=False)}")

    print("\n2) 建会话 /api/app/session（用户需求 → 分镜）")
    status, session = post(base, "app/session", {"text": "拍桌面青铜器：从偏低占高 40%，用 5 秒升到中部放大到 70%",
                                                 "mode": "fixture", "subject_height_mm": 200, "speed_pct": 40,
                                                 "host": args.robot})
    if status != 200 or not session.get("ok"):
        print("   失败:", json.dumps(session, ensure_ascii=False))
        return 1
    sid = session["session_id"]
    print(f"   HTTP {status}  session={sid}  总长={session['total_duration']}s  镜头数={len(session['shots'])}")
    for shot in session["shots"]:
        print(f"   #{shot['index']} {shot['shot_id']} {shot['shot_size_zh']}/{shot['camera_angle_zh']}/"
              f"{shot['camera_move_zh']} {shot['start']:.1f}-{shot['end']:.1f}s  曲线点={len(shot['target_curve'])}")

    print("\n3) 启动第一个镜头 /api/app/session/shot/start（真机动作需 confirm=true）")
    status, started = post(base, "app/session/shot/start", {"session_id": sid, "shot_id": None,
                                                            "host": args.robot, "confirm": True})
    if not started.get("ok"):
        print("   失败:", json.dumps(started, ensure_ascii=False))
        return 1
    print(f"   HTTP {status}  plan={started['plan_id']}  下发=" +
          "; ".join(f"{a['action_name']}{json.dumps(a['parameters'], ensure_ascii=False)}" for a in started["plan"]))
    target = session["shots"][0]["target_curve"][0]
    half_w = 0.1
    box = lambda dx=0.0, scale=1.0, dy=0.0: {                                  # noqa: E731
        "x1": target["center_x"] + dx - half_w,
        "y1": target["center_y"] + dy - target["subject_height_ratio"] * scale / 2,
        "x2": target["center_x"] + dx + half_w,
        "y2": target["center_y"] + dy + target["subject_height_ratio"] * scale / 2}

    print("\n4) 批量上报观测 /api/app/session/observe（一批 = 手机攒的几帧）")
    t0 = time.time()
    frames = [                                                                  # 一批 = 手机刚拍的连续帧
        {"timestamp": t0 - 0.45, "box": box()},
        {"timestamp": t0 - 0.34, "box": box(dx=0.12)},                          # 主体偏右
        {"timestamp": t0 - 0.23, "box": box(dy=0.08)},                          # 主体偏下
        {"timestamp": t0 - 0.12, "box": box(scale=0.8)},                        # 主体变小（离太远）
        {"timestamp": t0, "lost": True},                                        # 主体丢失
    ]
    status, observed = post(base, "app/session/observe", {"session_id": sid, "frames": frames,
                                                          "frame_width": 1920, "frame_height": 1080})
    print(f"   HTTP {status}  采纳 {len(observed['results'])} 帧")
    for index, row in enumerate(observed["results"]):
        line = (f"   帧{index} t={row['trajectory_time']:.2f}s 帧龄={row['age_s']}s "
                f"Gate={'accept' if row['gate']['accepted'] else row['gate']['code']} "
                f"决策={row['decision'] or '—'} {row['reason'] or ''}")
        if row["corrections"]:
            line += " 修正=" + "; ".join(
                f"{c['dimension']} {c.get('commanded_angle', c.get('travel_mm'))}°" if "commanded_angle" in c
                else f"{c['dimension']} {c.get('travel_mm')}mm" for c in row["corrections"])
        if row["deferred"]:
            line += f" [推迟：{row['deferred']}]"
        print(line)
        for entry in row["executed"] or []:
            if isinstance(entry, dict) and "action_name" in entry:
                print(f"        → {entry['action_name']} {json.dumps(entry['parameters'], ensure_ascii=False)}"
                      f" ok={entry['ok']}")
            elif isinstance(entry, dict):
                print(f"        → 停车 {json.dumps(entry, ensure_ascii=False)}")
    show("会话汇总", observed["summary"], ["frames", "decisions", "clock_offset"])

    print("\n5) 查状态 /api/app/session/state")
    status, state = post(base, "app/session/state", {"session_id": sid})
    show("状态", state["summary"], ["current_shot_id", "frames", "decisions"])
    print(f"   当前目标 {json.dumps(state.get('target'), ensure_ascii=False)}")

    print("\n6) 下一镜 /api/app/session/advance")
    status, advanced = post(base, "app/session/advance", {"session_id": sid, "host": args.robot, "confirm": True})
    if advanced.get("finished"):
        print("   分镜已拍完")
    else:
        print(f"   切到 {advanced['shot']['shot_id']}  下发=" +
              "; ".join(f"{a['action_name']}{json.dumps(a['parameters'], ensure_ascii=False)}" for a in advanced["plan"]))

    print("\n7) 错误路径抽查")
    status, bad = post(base, "app/session/observe", {"session_id": sid, "x1": 0.2, "y1": 0.2, "x2": 1.4, "y2": 0.8})
    print(f"   越界框 → HTTP {status} {bad.get('error', {}).get('code')} {bad.get('error', {}).get('reason')}")
    status, future = post(base, "app/session/observe", {"session_id": sid, "timestamp": time.time() + 30,
                                                       "x1": 0.4, "y1": 0.45, "x2": 0.6, "y2": 0.75})
    gate = (future.get("results") or [{}])[0].get("gate")
    print(f"   未来时间戳 → HTTP {status} {json.dumps(gate, ensure_ascii=False)}")
    for hint in future.get("hints", []):
        print(f"   提示：{hint}")
    status, noshot = post(base, "app/session/shot/start", {"session_id": "sess-nope"})
    print(f"   错会话 → HTTP {status} {noshot.get('error', {}).get('code')}")
    status, noconfirm = post(base, "app/session/shot/start", {"session_id": sid, "host": args.robot})
    print(f"   真机未确认 → HTTP {status} {noconfirm.get('error', {}).get('code')}")

    print("\n8) 停止 /api/app/session/stop")
    status, stopped = post(base, "app/session/stop", {"session_id": sid, "reason": "自检结束"})
    show("停车结果", stopped["stopped"])

    status, noc = post(base, "app/session/close", {"session_id": sid})
    show("关闭会话", noc, ["session_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
