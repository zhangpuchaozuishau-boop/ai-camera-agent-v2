"""End-to-end check of the web console API. Run with the server up: python webui/e2e_check.py"""
import json, sys, urllib.error, urllib.request

BASE = "http://127.0.0.1:8765/"


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path.lstrip("/"), timeout=30).read())


def post(name, body):
    req = urllib.request.Request(BASE + "api/" + name, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=250).read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def show_storyboard(r):
    sb = r["storyboard"]
    print(f"   {sb['title']} | 总长 {r['total_duration']}s | {len(sb['shots'])} 个镜头 | 警告 {len(r['warnings'])}")
    for t, s in zip(r["timeline"], sb["shots"]):
        kf = s["target_trajectory"]["keyframes"]
        print(f"    {t['start']:>5}-{t['end']:<5} {t['shot_size_zh']}/{t['camera_angle_zh']}/{t['camera_move_zh']}"
              f"( {s['move_speed']} ) h {kf[0]['frame_state']['subject_height_ratio']}->{kf[-1]['frame_state']['subject_height_ratio']} | {s['title']}")


if __name__ == "__main__":
    print("静态资源", {p: urllib.request.urlopen(BASE + p.lstrip("/"), timeout=10).status
                     for p in ("/", "/app.js", "/feedback.js", "/storyboard.js")})
    cfg = get("api/config")
    print("配置", cfg["base_url"], cfg["model"], "key:", cfg["api_key_present"])

    cases = [
        ("青铜器 20 秒展示", "给桌上的青铜器拍一段20秒的展示视频，要有大气感，最后落在纹饰细节上", {}),
        ("仓库标准用例", "拍摄固定桌面青铜器：从偏低、占画面高度40%，用5秒升到中部并放大到70%", {}),
        ("手机广告 30 秒", "为一个新款手机拍30秒的产品广告，开头要有悬念，中间展示侧面和摄像头，结尾正面logo", {}),
        ("要求环绕（已禁用）", "绕着茶壶转一圈拍10秒",
         {"allowed_moves": ["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down",
                            "pedestal_up", "pedestal_down"]}),
        ("离线 fixture", "任意文本", {"mode": "fixture"}),
    ]
    last = None
    for name, text, extra in cases:
        r = post("storyboard", {"text": text, "mode": "real", **extra})
        calls = r.get("provider_calls", [])
        print(f"[{'PASS' if r['ok'] else r['error']['code']}] {name} · {r['total_latency_s']}s · 调用 {len(calls)} 次 "
              f"{[c.get('latency_s') for c in calls]}")
        if r["ok"]:
            show_storyboard(r)
            last = last or r
        else:
            print("   ", r["error"].get("reason"), r["error"].get("context"))

    if last:
        shot = last["script"]["shots"][0]
        session = post("session", {"script": last["script"], "shot_id": shot["shot_id"]})
        print("会话", session["session_id"], session["state"]["status"], [e["status"] for e in session["events"]])
        k0 = shot["target_trajectory"]["keyframes"][0]["frame_state"]
        box = lambda cx, cy, h: {"x1": cx - .1, "x2": cx + .1, "y1": cy - h / 2, "y2": cy + h / 2}
        cx, cy, h = k0["center_x"], k0["center_y"], k0["subject_height_ratio"]
        rows = [("贴合目标", box(cx, cy, h)), ("偏移", box(cx + .1, cy + .06, h - .08)), ("丢失", None)]
        for label, b in rows:
            r = post("feedback", {"session_id": session["session_id"], "t": 0.0, "bbox": b})
            d = r["decision"]
            print("  Feedback", label, "->", d["decision"], d["reason"],
                  [c["dimension"] for c in (d.get("correction_intent") or {}).get("components", [])])
        print("拒绝示例：", post("session", {"script": last["script"], "shot_id": shot["shot_id"],
                                            "reachability": "UNREACHABLE"}).get("error", {}).get("code"))
