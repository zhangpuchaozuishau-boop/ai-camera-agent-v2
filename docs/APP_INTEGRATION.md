# App 接入接口（App → Agent → 机器人）

App 是入口：用户输入需求 → App 上报主体绿框坐标 → Agent 判断并驱动机器人。
**Agent 已经实现这套接口并可自检**（`webui/app_e2e_check.py`，全部走真实 HTTP）。

版本：`app-api-1.0`　基址：`http://<Agent 的 IP>:8765/api/app/`　传输：HTTP/1.1 + JSON

---

## 0. 网络前提

```
手机 App ──WiFi──┐
                 ├── Agent 服务（笔记本 / Mac mini，监听 0.0.0.0:8765）
机器人 ESP32 ────┘        │
 192.168.4.1 (SoftAP)     └── 机器人 http://192.168.4.1（Agent 直接下指令）
```

* Agent 启动：`.venv/Scripts/python.exe -m webui.server --port 8765`
  启动后会打印 **`App 入口 http://<IP>:8765/api/app/`**，把这个地址给 App。
* 手机要和 Agent 在同一网段（连机器人热点 `DustCar` 也行，只要 Agent 也连上）。
* 需要鉴权时启动加 `--token <字符串>`（或环境变量 `AGENT_APP_TOKEN`），之后所有 `/api/app/*`
  请求都要带 header `X-Agent-Token: <字符串>`；不带则 401 `UNAUTHORIZED`。
* Android 注意：`http://` 明文需要在 `AndroidManifest.xml` 里
  `android:usesCleartextTraffic="true"`，并申请 `INTERNET` 权限。

---

## 1. 一次拍摄的完整时序

```
App                                Agent                         机器人
 │  POST app/hello ────────────────▶│  标定状态/能力/机器人可达
 │  POST app/session ──────────────▶│  用户需求 → 分镜（1 次大模型调用）
 │   ◀── shots[]（每镜的目标曲线、容差、时长、运镜）
 │  POST app/session/shot/start ───▶│  初始动作 ──────────────────▶ 舵机/底盘
 │   ◀── plan[]（真正下发的指令）
 │  ┌ 循环：每秒 5~10 次 ─────────────────────────────────────┐
 │  │ POST app/session/observe ───▶ │ Gate → Feedback → 修正编译 │─▶ 舵机/底盘
 │  │  ◀── decision: CONTINUE / ADJUST / PAUSE + 实际发出的指令 │
 │  └────────────────────────────────────────────────────────┘
 │  POST app/session/advance ──────▶│  切换下一镜，重新 start
 │  POST app/session/stop ─────────▶│  物理停车（底盘 + 执行器）
```

大模型**只在 `session` 那一步调用一次**；观测闭环里没有任何模型推理（实测判决+编译中位 4.7ms）。

---

## 2. 端点一览

| # | 端点 | 作用 | 会动真机？ |
|---|---|---|---|
| 0 | `POST /api/app/ping`（也收 GET） | 最便宜的连通性检查：不探机器人、不扫标定文件，毫秒级 | 否 |
| 1 | `POST app/hello` | 握手：能力、安全上限、标定完成度、机器人可达 | 否（`probe:true` 只读状态） |
| 2 | `POST app/session` | 用户需求 → 分镜脚本 | 否 |
| 3 | `POST app/session/shot/start` | 开始拍某个镜头（下发初始动作） | **是**（需 `confirm:true`） |
| 4 | `POST app/session/observe` | 上报绿框（单帧或批量）→ 决策 + 修正 | **是**（ADJUST/PAUSE 时） |
| 5 | `POST app/session/state` | 查当前镜头、目标、舵机角度、决策序列 | 否 |
| 6 | `POST app/session/advance` | 结束当前镜头、开始下一镜 | **是** |
| 7 | `POST app/session/stop` | 物理停车 | **是** |
| 8 | `POST app/session/close` | 结束会话并释放 | 是（会先停车） |

统一：成功 `HTTP 200` + `{"ok": true, ...}`；失败 `HTTP 400/401/404` + `{"ok": false, "error": {"code": "...", "reason": "...", "context": {...}}}`。

### 2.1 App 的 HTTP 预算（按你的 0.6.0 实测对齐）

| 你的设置 | Agent 侧保证 | 实测 |
|---|---|---|
| 连接 1.5s | 局域网内 TCP 建连 < 10ms | `http://172.20.10.2:8765` 建连失败只可能是 Agent 没跑/不同网段 |
| 读 1.5s | **`hello` 最多等 `probe_wait_s`（默认 0.9s）**，其余探测留在后台；`ping` 不探测 | 机器人不可达：821ms；命中缓存：24ms；不探测：7–23ms |
| 总 3s | 其余端点都在毫秒–秒级（`session` 调大模型时最长，见 §5） | `observe` 经调试台 21.9ms 中位 |

**机器人探测不会卡住握手**：`hello` 里的 `probe:true` 不再同步等机器人。探测在后台线程里跑（客户端超时 0.8s），
`hello` 拿的是缓存；首次可能返回 `robot.state="checking"`、`reachable=null`（**这不是错误**），1 秒后再调一次就得到定论。

| `robot.state` | 含义 | App 该做什么 |
|---|---|---|
| `not_requested` | 没传 `probe` | 需要机器人信息就带 `probe:true` 再调 |
| `checking` | 后台正在探测（首次常见） | 展示"检测中"，1 秒后重调 `hello`；不要当失败 |
| `ready` | 可达，`status` 里有电量/模式 | 可以继续 |
| `unreachable` | 不可达，`error.code=DEVICE_UNREACHABLE` | 提示用户检查机器人电源与网络；**仍可先建会话演练** |
| `unsupported` | 该 Agent 没配探测 | 忽略机器人信息 |

`probe_wait_s` 可覆盖等待上限（例如 `{"probe":true,"probe_wait_s":1.2}`）；`hello` 的响应里带 `timings.total_ms`，
方便你判断是不是慢在 Agent 内部。`ping` 的响应形如
`{"ok":true,"api_version":"app-api-1.0","now":...,"sessions":0,"robot_state":"unknown|ready|unreachable"}`。

---

## 3. 逐个端点

### 3.1 `POST /api/app/hello` — 握手

```json
{"host": "192.168.4.1", "probe": true}
```
`probe` 为真时 Agent 会**在后台**读一次机器人 `/api/status`（客户端超时 0.8s），并把结果缓存约 10 秒；
本次响应最多等 `probe_wait_s`（默认 0.9s，见 §2.1），所以机器人离线也不会把握手拖过你的读超时。
首次可能返回 `robot.state="checking"`、`reachable=null`——这不是错误，1 秒后重调即可拿到 `ready` / `unreachable`。

响应（截断示例）
```json
{
  "ok": true, "api_version": "app-api-1.0", "agent": "ai-camera-agent-v2", "now": 1791011113.53,
  "calibration": {"ready": false, "measured": 1, "total": 32,
                  "missing": ["camera.fx_px", "chassis.mm_per_s_at_100pct", "..."],
                  "note": "未标定的常量仍是 placeholder：距离/角度的数值不可信，但链路可用"},
  "capabilities": {
    "shot_sizes": ["extreme_wide", "wide", "full", "medium", "medium_close", "close_up", "extreme_close_up"],
    "moves": ["static", "push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down",
              "pedestal_up", "pedestal_down", "truck_left", "truck_right", "arc_left", "arc_right",
              "orbit_left", "orbit_right", "follow", "zoom_in", "zoom_out"],
    "limits": {"max_shots": 8, "max_shot_duration_s": 15.0, "max_total_duration_s": 60.0,
               "max_single_move_mm": 800.0, "max_speed_pct": 60, "max_run_seconds": 120.0,
               "max_correction_mm": 300.0, "max_correction_deg": 12.0, "min_correction_interval_s": 0.4},
    "registry_revision": "dustcar-v1.2"
  },
  "robot": {"host": "192.168.4.1", "reachable": true,
            "status": {"mode": "idle", "battery_mv": 7400, "battery_ok": true, "speed": 0.0,
                       "meas": [0.0, 0.0, 0.0, 0.0], "pwm": [0, 0, 0, 0]}},
  "sessions": 0
}
```

App 应该：把 `calibration.ready` 为 `false` 时提示"距离/精度未经标定"；用 `capabilities.moves`
渲染可选运镜；用 `limits` 做本地输入校验，避免提交必被拒的请求。

### 3.2 `POST /api/app/session` — 用户需求 → 分镜

```json
{"text": "拍桌面青铜器：从偏低占高 40%，用 5 秒升到中部放大到 70%",
 "host": "192.168.4.1", "subject_height_mm": 200, "speed_pct": 40,
 "max_shots": 8, "max_shot_duration": 15, "max_total_duration": 60,
 "allowed_moves": ["push_in", "static"], "mode": "real"}
```
* `text`（必填）：用户原话。
* `host`：机器人地址（默认 `192.168.4.1`）。
* `subject_height_mm`：主体真实高度，距离换算必需；不给则用标定里的默认值。
* `speed_pct`：底盘速度百分比（上限见 `limits.max_speed_pct`）。
* `allowed_moves`：只允许这些运镜；不支持的运镜会被明确拒绝（不会偷偷替换）。
* `mode`：`real`（真调大模型）或 `fixture`（离线预设，联调用，0 次调用）。
* `dry_run`：true 则后续所有动作只编译不发送（整个会话默认演练）。
* `max_age_seconds`：观测帧龄上限（默认 1.0）。

响应
```json
{
  "ok": true, "session_id": "sess-a5451d57",
  "total_duration": 7.0,
  "warnings": [],
  "shots": [{
    "index": 0, "shot_id": "s1", "title": "走近青铜器",
    "purpose": "交代主体与环境", "frame_description": "青铜器在画面下部，占高约 40%",
    "shot_size": "wide", "shot_size_zh": "远景",
    "camera_angle": "low", "camera_angle_zh": "仰拍",
    "camera_move": "push_in", "camera_move_zh": "推", "move_speed": "slow",
    "transition_in": "fade_in", "rig_hint": "底盘前进 / 横梁伸出",
    "start": 0.0, "end": 4.0, "duration": 4.0,
    "target_curve": [
      {"t": 0.0, "center_x": 0.5, "center_y": 0.6, "subject_height_ratio": 0.3, "distance": null},
      {"t": 4.0, "center_x": 0.5, "center_y": 0.55, "subject_height_ratio": 0.5, "distance": null}],
    "tolerance": {"center_x_tolerance": 0.05, "center_y_tolerance": 0.05,
                  "height_ratio_tolerance": 0.05, "distance_tolerance": null}
  }],
  "subject_height_mm": 200, "speed_pct": 40, "host": "192.168.4.1", "dry_run": false
}
```
`target_curve` + `tolerance` 就是 App HUD 要画的目标框：某一时刻的目标框 =
`[center_x±0.1] × [center_y±subject_height_ratio/2]`，容差为 `±tolerance`（归一化）。

### 3.3 `POST /api/app/session/shot/start` — 开始一个镜头

```json
{"session_id": "sess-a5451d57", "shot_id": "s1", "host": "192.168.4.1",
 "speed_pct": 40, "subject_height_mm": 200, "confirm": true, "reachability": "REACHABLE"}
```
* `shot_id` 省略 / null → 用 `index`（当前镜头）；也可以 `index`。
* 真机会动，必须 `confirm: true`（否则 400 `CONFIRMATION_REQUIRED`）；演练用会话级 `dry_run`。
* `reachability`：`REACHABLE | UNREACHABLE | UNKNOWN`。`UNREACHABLE` 会在**下发之前**拒绝。

响应
```json
{"ok": true, "session_id": "sess-a5451d57", "plan_id": "loop-de4041ab",
 "shot": {"index": 0, "shot_id": "s1", "shot_goal": "…", "expected_duration": 4.0},
 "plan": [{"action_name": "servo_set", "parameters": {"id": 2, "angle": 88}},
          {"action_name": "chassis_drive", "parameters": {"dir": "fwd", "speed": 40, "duration_ms": 1543}}],
 "dry_run": false, "host": "192.168.4.1",
 "frame_contract": {"box": "...", "timestamp": "...", "lost": "...", "space": "..."}}
```
`plan` 就是真正发出去（或演练中本该发出）的指令；`frame_contract` 是给 App 看的字段约定提醒。

### 3.4 `POST /api/app/session/observe` — 上报观测（核心，建议 5–10 Hz）

单帧：
```json
{"session_id": "sess-a5451d57",
 "x1": 812, "y1": 340, "x2": 1180, "y2": 900,
 "frame_width": 1920, "frame_height": 1080,
 "timestamp": 4213.27}
```
批量（推荐，减少 WiFi 往返）：
```json
{"session_id": "sess-a5451d57", "frames": [
  {"timestamp": 4213.27, "box": {"x1": 812, "y1": 340, "x2": 1180, "y2": 900}},
  {"timestamp": 4213.47, "box": {"x1": 830, "y1": 340, "x2": 1198, "y2": 900}}],
 "frame_width": 1920, "frame_height": 1080}
```
主体丢失：
```json
{"session_id": "sess-a5451d57", "lost": true}
```
字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `x1,y1` | number | 是 | 绿框**左上** |
| `x2,y2` | number | 是 | 绿框**右下** |
| `frame_width/frame_height` | number | 像素坐标必填 | 画面尺寸（与检测框同坐标系） |
| `timestamp` | number | 建议 | 这一帧的拍摄时刻（秒）。**手机自己的时钟即可**，Agent 首次自动对齐；不要提前打时间戳 |
| `space` | string | 否 | `normalized` / `pixel`；不给则按数值判断（>2 视为像素） |
| `lost` | bool | 否 | 主体不在画面 → Feedback 判 PAUSE 并物理停车 |
| `distance` | number | 否 | 单位待定，**现在不参与决策** |
| `execute` | bool | 否 | false = 只判决不动作（干跑观测，便于回放） |
| `clock_offset_s` | number | 否 | 显式声明时基：`Agent 时间 = 手机时间 + clock_offset_s` |
| `reset_clock` | bool | 否 | 重新对齐时基（仅在你确知手机时钟变了时用） |
| `frames` | array | 否 | 批量上报，按顺序判决 |

响应
```json
{"ok": true, "session_id": "sess-a5451d57", "plan_id": "loop-de4041ab",
 "results": [{
   "trajectory_time": 0.11, "age_s": 0.34,
   "gate": {"accepted": true, "code": "ACCEPTED", "reason": "Fresh observation admitted"},
   "target": {"center_x": 0.5, "center_y": 0.5986, "subject_height_ratio": 0.3055, "distance": null},
   "box": {"x1": 0.52, "y1": 0.45, "x2": 0.72, "y2": 0.75},
   "decision": "ADJUST", "reason": "trajectory_offset",
   "correction_components": [{"dimension": "CENTER_X", "target_value": 0.5, "observed_value": 0.62, "error": 0.12}],
   "corrections": [{"dimension": "PAN", "delta_deg": 8.73, "requested_angle": 81.27,
                    "commanded_angle": 81, "clamped": false}],
   "executed": [{"action_id": "corr-ec0c01f4-0", "action_name": "servo_set",
                 "parameters": {"id": 1, "angle": 81}, "ok": true, "response": {"ok": true}, "seconds": 0.016}],
   "deferred": null}],
 "frames_total": 5, "hints": [],
 "summary": {"frames": 5, "decisions": ["CONTINUE", "ADJUST", "ADJUST", "ADJUST", "PAUSE"],
             "clock_offset": 0.018, "correction_budget": 3, "servo_angles": {"pan": 81, "tilt": 94}}
}
```

`decision` 三种：
* **CONTINUE**：在容差内（`within_trajectory_tolerance`），什么都没发。
* **ADJUST**：有偏差。`corrections` 是换算出的动作（云台角度 / 进退毫米+时长），`executed` 是实际下发与固件回执。
  两次修正之间最小间隔 0.4s（`limits.min_correction_interval_s`），太频繁的帧会进 `deferred`（这时 App 不必重发，等下一帧即可）。
* **PAUSE**：`target_lost`（主体丢失）/ `correction_budget_exhausted`（单镜头修正次数用尽）等；
  Agent 会立刻 `dir=stop` + `/api/stop` 物理停车，`executed` 是停车回执。
  恢复方式：App 重新 `shot/start`（或 `advance`）。

`hints` 是给人看的解释（如"帧龄过大""修正被推迟""时间戳在未来"）；判断逻辑不要依赖它。

### 3.5 `POST /api/app/session/state`

```json
{"session_id": "sess-a5451d57"}
```
返回 `summary`（镜头进度、帧数、决策序列、时基偏移、循环统计）、`target`（当前时刻的目标框）、
`servo_angles`、`last_frame`。App 用它做断线重连后的界面恢复。

### 3.6 `POST /api/app/session/advance`

```json
{"session_id": "sess-a5451d57", "confirm": true, "host": "192.168.4.1"}
```
先停当前镜头，再启动分镜里的下一个；全部拍完返回 `{"finished": true}`。
（只结束不开始，用 `session/stop`。）

### 3.7 `POST /api/app/session/stop`

```json
{"session_id": "sess-a5451d57", "reason": "用户点了停止"}
```
返回 `{"stopped": {"reason": "...", "chassis": {"ok": true}, "actuator": {"ok": true, "emergency": false}}}`。
这是**停车请求 + 固件回执**；固件没有"暂停确认"语义，别显示成"已确认暂停"。

### 3.8 `POST /api/app/session/close`

结束会话并释放（会先停车）。会话不存在后，继续用旧 `session_id` 会返回 400 `NO_SESSION`。

---

## 4. 错误码

| code | HTTP | 含义 | App 该怎么做 |
|---|---|---|---|
| `NO_SESSION` | 400 | 会话不存在（或已关闭/Agent 重启） | 重新 `session` |
| `SHOT_NOT_STARTED` | 400 | 还没 `shot/start` 就上报观测 | 先 start |
| `CONFIRMATION_REQUIRED` | 400 | 真机动作没带 `confirm:true` | 加确认，或改用 dry_run 演练 |
| `INVALID_PARAMETERS` | 400 | 字段缺失/类型错/值超范围 | 按 `context` 修字段；别重试同样的请求 |
| `INVALID_OBSERVATION` | 400 | 框越界/翻转、缺坐标 | 修检测输出；**不要**自己夹到合法范围 |
| `STALE` | 200（在 `gate.code` 里） | 帧太旧（超过 `max_age_seconds`） | 丢弃这一帧，继续上报新帧 |
| `UNREACHABLE` | 400 | 可达性判为不可达（下发前拒绝） | 换机位/换镜头，别重试 |
| `CAPABILITY_VIOLATION` | 400 | 动作超出注册表能力 | 别重试；报告给 Agent 侧 |
| `UNAUTHORIZED` | 401 | 缺/错 `X-Agent-Token` | 检查 token |
| `NOT_FOUND` / 404 | 404 | 路径不对 | 检查路径拼写 |

---

## 5. App 侧最小实现（Android / Kotlin）

```kotlin
object Agent {
    var base = "http://172.20.10.2:8765/api/app/"   // hello 里/启动打印出来的地址
    var token: String? = null

    fun post(path: String, body: String): JSONObject {
        val conn = (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"; doOutput = true
            setRequestProperty("Content-Type", "application/json")
            token?.let { setRequestProperty("X-Agent-Token", it) }
            connectTimeout = 3000; readTimeout = 15000
        }
        conn.outputStream.use { it.write(body.toByteArray()) }
        val text = (if (conn.responseCode < 400) conn.inputStream else conn.errorStream)
            .bufferedReader().readText()
        return JSONObject(text)          // 失败时含 error.code / error.reason
    }
}

// 1) 一次会话
val session = Agent.post("session", """{"text":"$userText","host":"192.168.4.1",
                                       "subject_height_mm":$subjectHeight,"speed_pct":40}""")
val sessionId = session.getString("session_id")

// 2) 开始第一个镜头
Agent.post("session/shot/start", """{"session_id":"$sessionId","confirm":true}""")

// 3) 观测循环：检测到绿框就上报（每 100~200ms 一批）
//    检测器输出像素框 → 直接给 x1,y1,x2,y2 + frame_width/frame_height
fun onDetected(box: Rect, frameW: Int, frameH: Int, capturedAtSec: Double) {
    val body = """{"session_id":"$sessionId","frames":[
        {"timestamp":$capturedAtSec,"x1":${box.left},"y1":${box.top},
         "x2":${box.right},"y2":${box.bottom}}],
        "frame_width":$frameW,"frame_height":$frameH}"""
    val r = Agent.post("session/observe", body)
    when (r.getJSONArray("results").getJSONObject(0).optString("decision")) {
        "CONTINUE" -> {}                       // 保持
        "ADJUST"   -> showHud("正在自动修正")   // Agent 已经动了，App 只做提示
        "PAUSE"    -> {                        // 主体丢了/修正次数用尽
            showAlert(r.getJSONArray("results").getJSONObject(0).optString("reason"))
        }
    }
}
// 主体丢失时：Agent.post("session/observe", """{"session_id":"$sessionId","lost":true}""")
// 结束时：Agent.post("session/stop", """{"session_id":"$sessionId"}""")
```

要点：
* **不要在 App 里算角度/毫米** —— 换算是 Agent 的事，App 只给像素框 + 画幅。
* 绿框坐标**原样上报**，越界/翻转会被明确拒绝（Agent 不会替你修）。
* 时间戳用手机自己的时钟即可；**拍摄后立刻上报**，不要提前打时间戳。
* 重连后先 `session/state` 拉状态，再继续 `observe`。

---

## 6. 联调自检（不用真机）

```bash
.venv/Scripts/python.exe -m webui.fake_firmware --port 8899   # 假固件
.venv/Scripts/python.exe -m webui.server --port 8765          # Agent
.venv/Scripts/python.exe webui/app_e2e_check.py               # 走完 8 步（含错误路径）
.venv/Scripts/python.exe webui/app_e2e_check.py --robot 192.168.4.1   # 换成真机
.venv/Scripts/python.exe webui/app_hello_probe.py --base http://172.20.10.2:8765
                                                              # 用 App 的超时预算发握手，量出耗时
```
`app_e2e_check.py` 会真实打印每一步的 HTTP 状态、决策、修正量和固件回执——手机接进来之前先用它确认 Agent 侧没问题。
`app_hello_probe.py` 发的是**和 App 一字不差的握手**（同样的 header、`{"host":...,"probe":true}`、连接 1.5s / 读 1.5s / 总 3s 预算），
并打印 `hello` 内部耗时、机器人状态与缓存命中情况——手机超时时用它对比。

### 6.1 手机连不上 / 超时怎么排查

| 现象 | 电脑端动作 | 结论 |
|---|---|---|
| App 立刻超时，且调试台第 9 块的"手机连接记录"**没有任何条目** | 确认 Agent 在跑（启动会打印 `App 入口 http://<IP>:8765/api/app/`） | 请求根本没到电脑：不同网段 / 防火墙 / 地址填错 |
| 记录里有条目但 `status` 是 401 | 检查是否用了 `--token` 启动 | 填对 `X-Agent-Token`，或不带 token 重启 |
| 记录里有条目但路径是 `/api/ping`、`/api/hello` 这类**少了 `/app` 段**的（实测鸿蒙 App 会这样） | Agent 已自动按 `/api/app/...` 处理，日志里会写"路径缺少 /app 段" | 能用了；但请把 App 的基址改成 `http://<IP>:8765/api/app/`，别名只是兼容网 |
| 记录里有条目、`status` 200、`ms` 很小 | 看 App 侧解析 | Agent 正常，问题在 App 的 JSON 解析/超时设置 |
| 记录里 `ms` 接近 1.5s | 跑 `app_hello_probe.py` 对比 | 若探测拖慢握手，把 `probe_wait_s` 调小或用 `ping` 做心跳 |

**路径容错**：`/api/ping`、`/api/hello`、`/api/session/observe` 这种漏掉 `/app` 一段的写法会被自动映射到
`/api/app/...`（带尾斜杠也收），并且**每一次替换都记进连接记录**——容错但不说谎。基址仍推荐写成 `…:8765/api/app/`。
`http://<IP>:8765/api/app/` 或 `/api/` 直接打开会返回一段"Agent 在线"的 JSON，可作为手机浏览器的连通性凭据。

调试台第 9 块底部会持续显示最近 6 条 App 请求（时间、方法、路径、状态、耗时、来源 IP、User-Agent），
也可以直接取 `POST /api/net/applog`。**手机一点"连接并使用"，这里就应该立刻出现一条 `POST app/hello → 200`。**

---

## 7. 现在的边界（不要当成已经完成的能力）

1. **标定未填**：32 项标定里目前只有 1 项（保活间隔，来自接口规范）。`mm_per_s_at_100pct`、`fx/fy`、
   画幅、舵机 id/零位/方向/减速比等仍是 placeholder → 距离与角度的**数值不可信**（链路是通的）。
   接口里 `calibration.ready=false` 就是这件事的机器可读形式。
2. **距离单位未定**：`distance` 字段收得下，但不参与决策。
3. **底盘开环**：固件只接受"方向+速度"，没有"走 N 毫米"；`/api/status.meas` 的脉冲换算系数未知，
   所以位移靠下一帧观测纠偏，而不是靠里程计。
4. **PAUSE = 停车请求**，不是"硬件确认已停"。
5. **无并发保护**：多个 App 同时连同一个 Agent 会互相打断；需要在 App 侧保证单入口，或后续加会话独占。
6. 真机首次联调请先 `dry_run`，再低速（`speed_pct` 20~30）短动作验证方向符号。
