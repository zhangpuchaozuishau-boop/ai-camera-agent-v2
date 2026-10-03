"""Calibration fields: unit, how to measure, valid range, and a validated writer.

The console shows CALIBRATION_FIELDS as a form; apply_values() writes the numbers you
measured into calibration.json, marks them verified, and keeps a backup. Nothing here
guesses a value: an entry is either still a placeholder or something you measured.
"""

import json
from pathlib import Path
import shutil
import time

from ..errors import AgentError
from .compiler import CALIBRATION_PATH

# key -> (单位, 怎么测, 影响)
CALIBRATION_FIELDS = {
    "camera.fx_px": ("像素", "用已知尺寸的标定板/格子，在固定距离拍一张，量同一水平线上两点的像素差：fx = 像素差 × 距离 ÷ 实际宽度", "水平角换算；错 → 云台修正量错"),
    "camera.fy_px": ("像素", "同上，纵向两点：fy = 像素差 × 距离 ÷ 实际高度", "俯仰角换算"),
    "camera.frame_width_px": ("像素", "App 传来的画面实际宽度（和检测框同一坐标系）", "归一化、角度换算"),
    "camera.frame_height_px": ("像素", "同上，高度", "归一化、距离换算"),
    "subject.default_height_mm": ("毫米", "默认主体真实高度（例如青铜器 200mm），App 也可以每个镜头单独给", "高度占比 ↔ 距离"),
    "chassis.mm_per_s_at_100pct": ("毫米/秒", "地板直线段，dir=fwd speed=100 保持 3 秒，用卷尺量实际位移 ÷ 3", "进退距离→时长；**最关键的一个**"),
    "chassis.deg_per_s_at_100pct": ("度/秒", "dir=rot_l speed=100 保持 3 秒，量底盘转过多少度 ÷ 3（用手机罗盘或地面标记）", "环绕/旋转类运镜"),
    "chassis.default_speed_pct": ("百分比", "你想让机器人自己跑时用的速度（10..100）", "所有底盘动作的默认速度"),
    "chassis.dir_close": ("枚举", "哪个方向让相机离主体更近：fwd 还是 back", "进退方向；搞反会顶上去"),
    "chassis.dir_far": ("枚举", "相反那个方向", "进退方向"),
    "chassis.keepalive_ms": ("毫秒", "不算标定：规范建议 150-200ms", "底盘保活节奏"),
    "axes.dolly.device": ("枚举", "距离变化由谁做：chassis 或 beam_stepper", "谁来拉远近"),
    "axes.lateral.device": ("枚举", "水平移动由谁做：pan_servo 或 chassis", "谁负责 center_x"),
    "axes.vertical.device": ("枚举", "垂直移动由谁做：tilt_servo 或 column_stepper", "谁负责 center_y"),
    "servos.pan.id": ("1 或 2", "云台水平那个舵机接在固件的哪个口", "改错会动错轴"),
    "servos.pan.zero_angle": ("度", "舵机让镜头正对前方时的角度（通常 90）", "角度基准"),
    "servos.pan.deg_per_unit": ("度/度", "镜头的实际转角 ÷ 舵机角度变化（有减速比时不是 1.0）", "修正量缩放"),
    "servos.pan.sign": ("+1 或 -1", "给舵机 +10 度，画面里主体往左还是往右；往左记 +1（u = cx0 - fx*tan(pan)）", "修正方向；搞反会越修越偏"),
    "servos.tilt.id": ("1 或 2", "云台俯仰那个舵机的口", "改错会动错轴"),
    "servos.tilt.zero_angle": ("度", "镜头水平时的舵机角度", "角度基准"),
    "servos.tilt.deg_per_unit": ("度/度", "镜头俯仰角 ÷ 舵机角度变化", "修正量缩放"),
    "servos.tilt.sign": ("+1 或 -1", "给舵机 +10 度，画面里主体往上还是往下；往下记 +1（v = cy0 + fy*tan(tilt)）", "修正方向"),
    "servos.safe_min_angle": ("度", "机械上不会撞到/憋住的最小角度（留余量）", "安全限位"),
    "servos.safe_max_angle": ("度", "最大角度", "安全限位"),
    "steppers.column.id": ("1 或 2", "立柱那根丝杆接在哪个口", "轴分配"),
    "steppers.column.mm_per_rev": ("毫米/转", "手动转一圈丝杆，量滑台走了多少（皮带传动 = 齿距 × 齿数）", "立柱行程"),
    "steppers.column.rpm": ("转/分", "立柱动作时用的转速（1..3000）", "立柱速度"),
    "steppers.column.dir_up": ("0 或 1", "哪个方向是上升", "立柱方向"),
    "steppers.beam.id": ("1 或 2", "横梁伸缩那根丝杆的口", "轴分配"),
    "steppers.beam.mm_per_rev": ("毫米/转", "同上，量横梁滑台", "横梁行程"),
    "steppers.beam.rpm": ("转/分", "横梁动作转速", "横梁速度"),
    "steppers.beam.dir_extend": ("0 或 1", "哪个方向是伸出", "横梁方向"),
}

# key -> (最小, 最大, 类型)  用来挡掉明显填错的值
RANGES = {
    "camera.fx_px": (50.0, 20000.0, float), "camera.fy_px": (50.0, 20000.0, float),
    "camera.frame_width_px": (64, 8192, int), "camera.frame_height_px": (64, 8192, int),
    "subject.default_height_mm": (1.0, 5000.0, float),
    "chassis.mm_per_s_at_100pct": (5.0, 5000.0, float), "chassis.deg_per_s_at_100pct": (1.0, 720.0, float),
    "chassis.default_speed_pct": (10, 100, int), "chassis.keepalive_ms": (50, 600, int),
    "chassis.dir_close": (None, None, "dir"), "chassis.dir_far": (None, None, "dir"),
    "axes.dolly.device": (None, None, "device"), "axes.lateral.device": (None, None, "device"),
    "axes.vertical.device": (None, None, "device"),
    "servos.pan.id": (1, 2, int), "servos.tilt.id": (1, 2, int),
    "servos.pan.zero_angle": (0, 180, int), "servos.tilt.zero_angle": (0, 180, int),
    "servos.pan.deg_per_unit": (0.05, 20.0, float), "servos.tilt.deg_per_unit": (0.05, 20.0, float),
    "servos.pan.sign": (-1, 1, "sign"), "servos.tilt.sign": (-1, 1, "sign"),
    "servos.safe_min_angle": (0, 179, int), "servos.safe_max_angle": (1, 180, int),
    "steppers.column.id": (1, 2, int), "steppers.beam.id": (1, 2, int),
    "steppers.column.mm_per_rev": (1.0, 500.0, float), "steppers.beam.mm_per_rev": (1.0, 500.0, float),
    "steppers.column.rpm": (1, 3000, int), "steppers.beam.rpm": (1, 3000, int),
    "steppers.column.dir_up": (0, 1, "bit"), "steppers.beam.dir_extend": (0, 1, "bit"),
}

DIRECTIONS = ("fwd", "back", "left", "right", "rot_l", "rot_r")
DEVICES = ("chassis", "beam_stepper", "column_stepper", "pan_servo", "tilt_servo")


def _coerce(key, value):
    low, high, kind = RANGES[key]
    if kind == "dir":
        if value not in DIRECTIONS:
            raise AgentError("INVALID_PARAMETERS", f"{key} 必须是底盘方向之一", {"allowed": list(DIRECTIONS), "value": value})
        return value
    if kind == "device":
        if value not in DEVICES:
            raise AgentError("INVALID_PARAMETERS", f"{key} 必须是已声明的执行机构", {"allowed": list(DEVICES), "value": value})
        return value
    if kind == "sign":
        if int(value) not in (-1, 1):
            raise AgentError("INVALID_PARAMETERS", f"{key} 只能是 +1 或 -1", {"value": value})
        return int(value)
    if kind == "bit":
        if int(value) not in (0, 1):
            raise AgentError("INVALID_PARAMETERS", f"{key} 只能是 0 或 1", {"value": value})
        return int(value)
    try:
        number = kind(value)
    except (TypeError, ValueError) as exc:
        raise AgentError("INVALID_PARAMETERS", f"{key} 不是数字", {"value": value}) from exc
    if not low <= number <= high:
        raise AgentError("INVALID_PARAMETERS", f"{key} 超出合理范围 {low}..{high}", {"value": number})
    return number


def _resolve(data, key):
    """Return (node, field_name, kind) for a dotted key, or (None, None, None).

    kind 'leaf'  -> the key names a {value} / {device} block
    kind 'field' -> the key names one scalar inside a measured group (servos.pan.id)
    """
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            break
    if isinstance(node, dict):
        child = node.get(parts[-1])
        if isinstance(child, dict) and ("value" in child or "device" in child):
            return node, parts[-1], "leaf"
        if isinstance(child, (int, float, str)) and any(k in node for k in ("verified", "measured")):
            return node, parts[-1], "field"
    if len(parts) >= 2:  # group at the second-to-last position
        group = data
        for part in parts[:-2]:
            group = group.get(part) if isinstance(group, dict) else None
            if group is None:
                return None, None, None
        candidate = group.get(parts[-2]) if isinstance(group, dict) else None
        if isinstance(candidate, dict) and isinstance(candidate.get(parts[-1]), (int, float, str)):
            return candidate, parts[-1], "field"
    return None, None, None


def apply_values(values: dict, *, source: str, path=None) -> dict:
    """Validate every value, then write them as measured constants in one pass."""
    path = Path(path or CALIBRATION_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    unknown = [key for key in values if key not in CALIBRATION_FIELDS]
    if unknown:
        raise AgentError("INVALID_PARAMETERS", "未知的标定项", {"unknown": unknown})
    applied, problems = {}, {}
    for key, raw in values.items():
        node, field, kind = _resolve(data, key)
        if node is None:
            problems[key] = "calibration.json 里找不到这一项"
            continue
        try:
            value = _coerce(key, raw)
        except AgentError as error:
            problems[key] = error.reason
            continue
        if kind == "leaf":
            leaf = node[field]
            leaf["value" if "value" in leaf else "device"] = value
            leaf["verified"] = True
            leaf["source"] = source
        else:
            node[field] = value
            node.setdefault("measured", {})[field] = time.strftime("%Y-%m-%d %H:%M")
            scalars = [k for k, v in node.items()
                       if isinstance(v, (int, float, str)) and k not in ("verified", "source", "note")]
            node["verified"] = all(name in node["measured"] for name in scalars)
            node["source"] = source if node["verified"] else node.get("source", source)
        applied[key] = value
    if applied:
        shutil.copyfile(path, path.with_suffix(".json.bak"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": not problems, "applied": applied, "problems": problems,
            "written_at": time.strftime("%Y-%m-%d %H:%M:%S"), "backup": str(path.with_suffix(".json.bak")),
            "still_unverified": _remaining(path)}


def field_state(path=None) -> list[dict]:
    """Everything the console form needs: current value, unit, how to measure, measured?"""
    data = json.loads(Path(path or CALIBRATION_PATH).read_text(encoding="utf-8"))
    rows = []
    for key, (unit, how, effect) in CALIBRATION_FIELDS.items():
        node, field, kind = _resolve(data, key)
        if node is None:
            continue
        if kind == "leaf":
            leaf = node[field]
            value, measured = leaf.get("value", leaf.get("device")), bool(leaf.get("verified"))
        else:
            value, measured = node.get(field), field in (node.get("measured") or {})
        rows.append({"key": key, "unit": unit, "how": how, "effect": effect,
                     "value": value, "measured": measured})
    return rows


def _remaining(path):
    from .compiler import DustCarCalibration

    return DustCarCalibration.load(path).unverified()


def worksheet(path=None) -> str:
    """Markdown checklist you can fill by hand and paste back as JSON."""
    rows = field_state(path)
    groups = {}
    for row in rows:
        parts = row["key"].split(".")
        group = ".".join(parts[:-1]) or parts[0]
        groups.setdefault(group, []).append(row)
    lines = ["# DustCar 标定清单（填完可以发给我，或直接在网页调试台第 8 块里填）", "",
             "每一项都注明单位和测法；没测的保持 placeholder，我不会假装它已标定。", ""]
    for group, items in groups.items():
        lines += [f"## {group}", "", "| 常量 | 当前值 | 单位 | 怎么测 | 影响 | 实测值 |", "|---|---|---|---|---|---|"]
        for row in items:
            lines.append(f"| `{row['key']}` | {row['value']} | {row['unit']} | {row['how']} | {row['effect']} | "
                         f"{'（已标定）' if row['measured'] else ''} |")
        lines.append("")
    measured = sum(1 for row in rows if row["measured"])
    lines += [f"**进度：{measured}/{len(rows)} 项已标定。**", ""]
    return "\n".join(lines)
