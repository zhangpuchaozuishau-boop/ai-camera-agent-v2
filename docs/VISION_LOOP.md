# 视觉闭环接入：App 上报绿框 → 实时修正

> 手机 App 面向的完整接口（握手/会话/镜头/观测/停止 + Kotlin 示例）见
> [APP_INTEGRATION.md](APP_INTEGRATION.md)。本文讲 Agent 内部这条闭环怎么算的。

Agent 侧已经能消费你们视觉模块的**主体绿框左上/右下坐标**，并把它变成真机动作。
闭环里**不调用大模型**（大模型只在生成分镜时用一次），所以延时是编译+HTTP 的量级，
不是模型推理的量级。实测（本机、Windows、Python）：

| 环节 | 中位 | 样本范围 |
|---|---|---|
| Gate + Feedback + 修正编译（进程内，不含下发） | **4.7 ms** | 0.15 – 38.6 ms（抖动来自解释器/系统调度，不是算法） |
| 同上 + 舵机 HTTP 下发（打本机假固件） | **3.2 ms** | 2.0 – 30.9 ms |
| 经调试台 HTTP 端点的一次观测（JSON 往返） | **21.9 ms** | 3.1 – 49.3 ms |

真正的瓶颈是机械动作与帧率：底盘一次进退 0.5–1.5 s，舵机一趟十几毫秒，观测建议 ≥5 fps。

## 1. 一轮闭环长什么样

```
App 视觉模块 ──绿框(tl,br)+时间戳──▶ 观测归一化 ──▶ Gate ──▶ Feedback V2
                                                              │
                        CONTINUE / ADJUST / PAUSE ◀───────────┘
                              │
              ADJUST → 修正编译（画面偏差 → 舵机角度 / 进退毫米）→ 真机 HTTP
              PAUSE  → /api/stop + dir=stop（物理停车）
```

## 2. 接口

### 启动（每个镜头一次）

```
POST http://<agent>:8765/api/hw/loop/start
{
  "script": <ShotScript 0.2>,     # 分镜脚本（App 也可以自己生成）
  "shot_id": "s1",
  "subject_height_mm": 200,        # 主体真实高度，距离换算必需
  "speed_pct": 40,                 # 底盘速度百分比
  "host": "192.168.4.1",           # 机器人地址（默认 192.168.4.1；演练用 127.0.0.1:8899）
  "confirm": true,                 # 真机动作必须显式确认
  "dry_run": false                 # true = 只编译不下发
}
→ {"ok": true, "loop_id": "loop-xxxxxxxx", "plan": [...动作...], "frame_contract": {...}}
```

### 每一帧（App 持续上报，建议 ≥5 fps）

```
POST http://<agent>:8765/api/hw/loop/observe
{
  "loop_id": "loop-xxxxxxxx",
  "x1": 812, "y1": 340, "x2": 1180, "y2": 900,   # 绿框 左上/右下
  "frame_width": 1920, "frame_height": 1080,      # 像素坐标时必填
  "timestamp": 1234.56,                           # 这一帧的拍摄时刻（秒）
  "distance": null                                # 可选，单位待定
}
```

其它写法：
* 已经归一化到 `0..1` 的框：坐标都 ≤1 即可，或用 `"space": "normalized"` 明确指定。
* 明确像素：`"space": "pixel"`（比自动判断更保险）。
* 主体丢失：`{"loop_id": "...", "lost": true}`（或 bbox 传 null）。

响应里包含：`gate`（是否被采纳/为什么丢弃）、`decision`（CONTINUE/ADJUST/PAUSE + 原因）、
`corrections`（换算出的修正量：舵机角度或进退毫米）、`executed`（实际下发的指令与固件回执）。

### 汇总与停止

```
POST /api/hw/loop/summary  {"loop_id": "..."}   # 帧数、决策序列、修正次数、累计 HTTP 条数、当前舵机角度
POST /api/hw/loop/stop     {"loop_id": "..."}   # 立即停车（底盘 dir=stop + /api/stop）
```

## 3. 坐标与时间约定（**必须先对齐这三项**）

| 项 | Agent 的假设 | 需要你们确认 |
|---|---|---|
| 归一化 | `x = 像素x / frame_width`，`y = 像素y / frame_height`，原点在**左上**，y 向下 | 你们的框原点与 y 方向是否一致 |
| 时间戳 | 秒（浮点），与 Agent 的时钟同一域；`now - timestamp` 超过 `max_age_seconds`（默认 1.0s）判 `STALE` 丢弃 | 用谁的时间基准（建议 App 以自己的单调时钟为基准，并告知 Agent 一次零点） |
| 距离 | 目前**不启用**：单位未定，Feedback 不会因为它移动机器人 | 单位、缺失表达（null 还是 -1） |

框本身**不做修补**：越界（`x2 > frame_width`）或翻转（`x2 ≤ x1`）的框会被拒绝并返回
`INVALID_OBSERVATION`，不会偷偷夹到合法范围——因为用错的数据动真机比不动更危险。

## 4. 修正怎么算（画面偏差 → 机械量）

| 偏差分量 | 换成什么 | 公式/常量 |
|---|---|---|
| `CENTER_X` | 云台水平舵机角度 | `pan = atan((0.5 − cx)·frame_w / fx)`；Δpan = pan(目标) − pan(实测) |
| `CENTER_Y` | 云台俯仰舵机角度 | `tilt = atan((cy − 0.5)·frame_h / fy)` |
| `SUBJECT_HEIGHT_RATIO` | 进退 | `距离 = 主体真实高度·fy / (frame_h·高度占比)`，ΔD → 毫米 → 时长 |
| `DISTANCE` | 进退 | 直接毫米差（需距离目标启用且单位确定） |

安全约束（`calibration.json`，可改）：单次修正 ≤300mm / ≤12°，两次修正最小间隔 0.4s，
单镜头修正次数上限（默认 3，超过就 PAUSE），舵机限位 0..180°，底盘单次动作 ≤800mm。

## 5. 现在的边界

* 19 个标定常量仍是 placeholder（`calibration.json` 里 `verified=false`）→ 距离/角度**数值不可信**；
  公式与链路是通的，填入实测值后即可用。调试台第 8 块可逐项填写并自动备份。
* 底盘**开环**：固件只接受"方向+速度"，没有"走 N 毫米"；`/api/status.meas` 有脉冲增量但换算系数未知。
  闭环依赖 App 的下一帧观测来纠偏，不依赖底盘自己知道走了多远。
* Feedback 判 PAUSE 时下发的是**停车请求**，不是"固件确认已停"。
