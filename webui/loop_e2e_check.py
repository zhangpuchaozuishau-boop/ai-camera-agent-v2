"""End-to-end check of the vision closed loop against the fake firmware.

  .venv/Scripts/python.exe -m webui.fake_firmware --port 8899
  .venv/Scripts/python.exe -m webui.server --port 8765
  .venv/Scripts/python.exe webui/loop_e2e_check.py

No LLM call (storyboard comes from the offline fixture). Every agent command goes
over real HTTP to the fake firmware, so the printed table is what the rig received.
"""

import json
import sys
import time
import urllib.error
import urllib.request

CONSOLE = "http://127.0.0.1:8765/api/"
FIRMWARE = "127.0.0.1:8899"


def call(name, body=None):
    request = urllib.request.Request(CONSOLE + name, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"ok": False, "error": json.loads(error.read().decode())}


def main():
    board = call("storyboard", {"mode": "fixture", "text": "拍桌面青铜器，5 秒"})
    if not board.get("ok"):
        print("分镜失败:", board)
        return 1
    script = board["script"]
    shot = script["shots"][0]
    key = shot["target_trajectory"]["keyframes"][0]["frame_state"]
    print(f"离线分镜：{len(script['shots'])} 个镜头，第一个 {shot['shot_id']} {shot.get('shot_goal')}")
    print(f"t=0 目标：center_x={key['center_x']} center_y={key['center_y']} 高度占比={key['subject_height_ratio']}")

    start = call("hw/loop/start", {"script": script, "shot_id": shot["shot_id"], "host": FIRMWARE,
                                   "subject_height_mm": 200, "speed_pct": 40, "confirm": True})
    if not start.get("ok"):
        print("启动循环失败:", start)
        return 1
    loop = start["loop_id"]
    print("初始计划：", json.dumps(start["plan"], ensure_ascii=False))
    print("帧契约：", json.dumps(start["frame_contract"], ensure_ascii=False)[:120], "…\n")

    half_w = 0.1

    def box(offset_x=0.0, offset_y=0.0, scale=1.0):
        h = key["subject_height_ratio"] * scale
        return {"x1": key["center_x"] + offset_x - half_w, "y1": key["center_y"] + offset_y - h / 2,
                "x2": key["center_x"] + offset_x + half_w, "y2": key["center_y"] + offset_y + h / 2}

    steps = [
        ("完全贴合目标", box(), None),
        ("主体偏右 0.12", box(offset_x=0.12), None),
        ("主体偏下 0.08", box(offset_y=0.08), None),
        ("主体变小 20%（离得太远）", box(scale=0.8), None),
        ("主体丢失", None, True),
    ]
    for index, (label, frame, lost) in enumerate(steps):
        if index:
            time.sleep(0.5)          # let the 0.4s minimum-correction interval pass

        payload = {"loop_id": loop, "lost": True} if lost else {"loop_id": loop, **frame, "space": "normalized"}
        row = call("hw/loop/observe", payload)
        if not row.get("ok"):
            print(f"{label:22s} 请求被拒：{row['error'].get('code')} {row['error'].get('reason')}")
            continue
        gate, decision = row["gate"], row["decision"]
        line = f"{label:22s} Gate={'accept' if gate['accepted'] else gate['code']}" \
               f" 决策={decision['decision'] if decision else '—'}"
        if decision and decision.get("reason"):
            line += f" ({decision['reason']})"
        if row.get("corrections"):
            line += " 修正=" + "; ".join(
                f"{c['dimension']} {c.get('commanded_angle', c.get('travel_mm'))}"
                f"{'（限幅）' if c.get('clamped') else ''}" for c in row["corrections"])
        if row.get("deferred"):
            line += f" [推迟：{row['deferred']}]"
        print(line)
        stopped = row.get("executed")
        if isinstance(stopped, dict):                      # PAUSE path: physical stop result
            print(f"{'':24s}→ 停车 {json.dumps(stopped, ensure_ascii=False)}")
        for entry in (stopped if isinstance(stopped, list) else []):
            print(f"{'':24s}→ {entry['action_name']} {json.dumps(entry['parameters'], ensure_ascii=False)}"
                  f" ok={entry['ok']} resp={json.dumps(entry['response'], ensure_ascii=False)}"
                  f" ({entry.get('seconds')}s)")

    summary = call("hw/loop/summary", {"loop_id": loop})["summary"]
    print("\n循环汇总：", json.dumps(summary, ensure_ascii=False))
    state = json.loads(urllib.request.urlopen("http://" + FIRMWARE + "/api/status", timeout=5).read().decode())
    print("底盘当前状态：", json.dumps(state, ensure_ascii=False))
    stop = call("hw/loop/stop", {"loop_id": loop})
    print("停止：", json.dumps(stop.get("result"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
