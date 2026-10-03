"""Turn evals/runs/<date>.jsonl into a readable markdown report.

    .venv/Scripts/python.exe evals/report.py [--date 20261003] [--out evals/REPORT.md]
"""

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evals" / "runs"


def load(day):
    path = RUNS / f"{day}.jsonl"
    if not path.is_file():
        raise SystemExit(f"no results at {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="Render the capability eval results")
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--out", default=str(ROOT / "evals" / "REPORT.md"))
    args = parser.parse_args()
    records = load(args.date)
    # Only report cases that are still in the eval set; renamed/retired cases stay in
    # the raw JSONL as history but must not inflate the summary.
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from evals.agent_capability_check import CASES
        current = {case["name"] for case in CASES}
        records = [r for r in records if r["name"] in current]
    except Exception:
        pass
    # keep the newest run of each case name
    latest = {}
    for record in records:
        latest[record["name"]] = record
    records = list(latest.values())

    lines = [f"# Agent 能力评测报告（{args.date}）", "",
             f"- 用例数：{len(records)}，全部通过：{sum(1 for r in records if r['passed'])}",
             f"- 模型：{records[0]['model'] if records else '-'}",
             f"- 真实调用：{sum(len(r['provider_calls']) for r in records)} 次，"
             f"其中触发修复重试的用例：{sum(1 for r in records if len(r['provider_calls']) > 1)}",
             f"- 总延迟：{sum(r['elapsed'] for r in records):.1f}s，"
             f"单例中位 {sorted(r['elapsed'] for r in records)[len(records)//2]:.1f}s", ""]

    lines += ["## 用例结果", "", "| 用例 | 结果 | 检查项 | 延迟 | 调用 |", "|---|---|---|---|---|"]
    for record in records:
        passed = sum(1 for _, p, _ in record["checks"] if p)
        lines.append(f"| {record['name']} | {'PASS' if record['passed'] else 'FAIL'} | {passed}/{len(record['checks'])} "
                     f"| {record['elapsed']}s | {len(record['provider_calls'])} |")

    lines += ["", "## 逐条检查明细", ""]
    for record in records:
        lines += [f"### {record['name']}", "", f"> 需求：{record['text']}", ""]
        if record.get("error"):
            lines += [f"- 未生成分镜：`{record['error']['code']}` {record['error']['reason']}", ""]
        for label, ok, detail in record["checks"]:
            lines.append(f"- {'✅' if ok else '❌'} {label}" + (f" — {detail}" if detail else ""))
        if record.get("storyboard"):
            board = record["storyboard"]
            lines += ["", f"**{board['title']}** · 风格 {board['style']} · 主体 {board['subject']['name']}", "",
                      "| # | 时间 | 景别 | 机位 | 运镜 | 时长 | 高度占比 起→止 | 说明 |", "|---|---|---|---|---|---|---|---|"]
            for row in record["timeline"]:
                shot = board["shots"][row["index"] - 1]
                keys = shot["target_trajectory"]["keyframes"]
                lines.append(f"| {row['index']} | {row['start']}–{row['end']}s | {row['shot_size_zh']} | "
                             f"{row['camera_angle_zh']} | {row['camera_move_zh']} | {shot['duration']}s | "
                             f"{keys[0]['frame_state']['subject_height_ratio']} → "
                             f"{keys[-1]['frame_state']['subject_height_ratio']} | {shot['title']} |")
            if record.get("hardware_actions"):
                hardware = record["hardware_actions"]
                lines += ["", f"首个镜头编译出的真机动作（dry-run，累计 {hardware['commanded_seconds']}s / "
                              f"上限 {hardware['max_run_seconds']}s）：", ""]
                for action in hardware["actions"]:
                    lines.append(f"- `{action['action_name']}` {json.dumps(action['parameters'], ensure_ascii=False)}")
            if record.get("feedback"):
                lines += ["", f"Feedback 判定：{json.dumps(record['feedback'], ensure_ascii=False)}"]
        lines.append("")

    failures = [r["name"] for r in records if not r["passed"]]
    lines += ["## 结论", "", f"- 失败用例：{'、'.join(failures) if failures else '无'}", "",
              "- 说明：硬件步骤为 dry-run（只编译、不发真机）；可达性为 MOCK；未标定常量见 agent_system/hardware/calibration.json。"]
    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(records)} cases, {sum(1 for r in records if r['passed'])} passed)")
    stats = defaultdict(int)
    for record in records:
        for label, ok, _ in record["checks"]:
            stats[(label.split("=")[0].split("（")[0].strip(), ok)] += 1
    print("检查项通过率：")
    labels = sorted({label for label, _ in stats})
    for label in labels:
        ok, bad = stats[(label, True)], stats[(label, False)]
        print(f"  {label:<40} {ok}/{ok + bad}")


if __name__ == "__main__":
    main()
