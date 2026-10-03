# DustCar 标定清单（填完可以发给我，或直接在网页调试台第 8 块里填）

每一项都注明单位和测法；没测的保持 placeholder，我不会假装它已标定。

## camera

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `camera.fx_px` | 1500.0 | 像素 | 用已知尺寸的标定板/格子，在固定距离拍一张，量同一水平线上两点的像素差：fx = 像素差 × 距离 ÷ 实际宽度 | 水平角换算；错 → 云台修正量错 |  |
| `camera.fy_px` | 1500.0 | 像素 | 同上，纵向两点：fy = 像素差 × 距离 ÷ 实际高度 | 俯仰角换算 |  |
| `camera.frame_width_px` | 1920 | 像素 | App 传来的画面实际宽度（和检测框同一坐标系） | 归一化、角度换算 |  |
| `camera.frame_height_px` | 1080 | 像素 | 同上，高度 | 归一化、距离换算 |  |

## subject

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `subject.default_height_mm` | 200.0 | 毫米 | 默认主体真实高度（例如青铜器 200mm），App 也可以每个镜头单独给 | 高度占比 ↔ 距离 |  |

## chassis

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `chassis.mm_per_s_at_100pct` | 600.0 | 毫米/秒 | 地板直线段，dir=fwd speed=100 保持 3 秒，用卷尺量实际位移 ÷ 3 | 进退距离→时长；**最关键的一个** |  |
| `chassis.deg_per_s_at_100pct` | 90.0 | 度/秒 | dir=rot_l speed=100 保持 3 秒，量底盘转过多少度 ÷ 3（用手机罗盘或地面标记） | 环绕/旋转类运镜 |  |
| `chassis.default_speed_pct` | 40 | 百分比 | 你想让机器人自己跑时用的速度（10..100） | 所有底盘动作的默认速度 |  |
| `chassis.dir_close` | fwd | 枚举 | 哪个方向让相机离主体更近：fwd 还是 back | 进退方向；搞反会顶上去 |  |
| `chassis.dir_far` | back | 枚举 | 相反那个方向 | 进退方向 |  |
| `chassis.keepalive_ms` | 180 | 毫秒 | 不算标定：规范建议 150-200ms | 底盘保活节奏 | （已标定） |

## axes.dolly

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `axes.dolly.device` | chassis | 枚举 | 距离变化由谁做：chassis 或 beam_stepper | 谁来拉远近 |  |

## axes.lateral

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `axes.lateral.device` | pan_servo | 枚举 | 水平移动由谁做：pan_servo 或 chassis | 谁负责 center_x |  |

## axes.vertical

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `axes.vertical.device` | tilt_servo | 枚举 | 垂直移动由谁做：tilt_servo 或 column_stepper | 谁负责 center_y |  |

## servos.pan

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `servos.pan.id` | 1 | 1 或 2 | 云台水平那个舵机接在固件的哪个口 | 改错会动错轴 |  |
| `servos.pan.zero_angle` | 90 | 度 | 舵机让镜头正对前方时的角度（通常 90） | 角度基准 |  |
| `servos.pan.deg_per_unit` | 1.0 | 度/度 | 镜头的实际转角 ÷ 舵机角度变化（有减速比时不是 1.0） | 修正量缩放 |  |
| `servos.pan.sign` | -1 | +1 或 -1 | 给舵机 +10 度，画面里主体往左还是往右；往左记 +1（u = cx0 - fx*tan(pan)） | 修正方向；搞反会越修越偏 |  |

## servos.tilt

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `servos.tilt.id` | 2 | 1 或 2 | 云台俯仰那个舵机的口 | 改错会动错轴 |  |
| `servos.tilt.zero_angle` | 90 | 度 | 镜头水平时的舵机角度 | 角度基准 |  |
| `servos.tilt.deg_per_unit` | 1.0 | 度/度 | 镜头俯仰角 ÷ 舵机角度变化 | 修正量缩放 |  |
| `servos.tilt.sign` | -1 | +1 或 -1 | 给舵机 +10 度，画面里主体往上还是往下；往下记 +1（v = cy0 + fy*tan(tilt)） | 修正方向 |  |

## servos

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `servos.safe_min_angle` | 5 | 度 | 机械上不会撞到/憋住的最小角度（留余量） | 安全限位 |  |
| `servos.safe_max_angle` | 175 | 度 | 最大角度 | 安全限位 |  |

## steppers.column

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `steppers.column.id` | 1 | 1 或 2 | 立柱那根丝杆接在哪个口 | 轴分配 |  |
| `steppers.column.mm_per_rev` | 40.0 | 毫米/转 | 手动转一圈丝杆，量滑台走了多少（皮带传动 = 齿距 × 齿数） | 立柱行程 |  |
| `steppers.column.rpm` | 600 | 转/分 | 立柱动作时用的转速（1..3000） | 立柱速度 |  |
| `steppers.column.dir_up` | 1 | 0 或 1 | 哪个方向是上升 | 立柱方向 |  |

## steppers.beam

| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |
|---|---|---|---|---|---|
| `steppers.beam.id` | 2 | 1 或 2 | 横梁伸缩那根丝杆的口 | 轴分配 |  |
| `steppers.beam.mm_per_rev` | 40.0 | 毫米/转 | 同上，量横梁滑台 | 横梁行程 |  |
| `steppers.beam.rpm` | 600 | 转/分 | 横梁动作转速 | 横梁速度 |  |
| `steppers.beam.dir_extend` | 0 | 0 或 1 | 哪个方向是伸出 | 横梁方向 |  |

**进度：1/32 项已标定。**
