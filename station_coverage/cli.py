"""命令行入口：读取台账，输出可签认的阶段安排、退站决策和现场摘要。

用法::

    python3 -m station_coverage.cli [台账目录] [--show MODE]

``--show`` 可选 ``summary``（默认）、``stages``、``retirements``、
``common-mode``、``trials``、``json``。所有输出都是只读的；本工具不会修改
台账。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import LedgerError, load_ledger
from .plan import build_plan


def _format_stage(stage: dict[str, object], index: int) -> str:
    window = stage["window"]
    lines = [
        f"阶段 {index}: 第 {window[0]}–{window[1]} 天  [{stage['status']}]",
        f"  运行: {', '.join(stage['active']) or '无'}",
        f"  可计入覆盖: {', '.join(stage['credited']) or '无'}",
    ]
    if stage["retirements_completed"]:
        lines.append(f"  已完成退站: {', '.join(stage['retirements_completed'])}")
    for item in stage["capability"]:
        marker = "可以" if item["capable"] else "不足"
        lines.append(
            f"  {item['area']}/{item['stratum']}: 独立 {item['independent']}/{item['required_independent']} "
            f"({marker}) 使用 {', '.join(station['station'] for station in item['stations']) or '无'}"
        )
        if not item["capable"] and item["reason"]:
            lines.append(f"      阻断: {item['reason']}")
    for blocker in stage["blockers"]:
        lines.append(
            f"  阻断[{blocker['type']}]: {blocker['detail']} "
            f"({', '.join(blocker.get('areas', []))}/{', '.join(blocker.get('strata', []))})"
        )
    for caveat in stage["caveats"]:
        label = "覆盖提示" if caveat["type"] == "coverage_floor" else "证据空白"
        lines.append(
            f"  {label}: {', '.join(caveat.get('areas', []))}/"
            f"{', '.join(caveat.get('strata', []))} — {caveat['detail']}"
        )
        if caveat.get("reasons"):
            for reason, opportunities in sorted(caveat["reasons"].items()):
                lines.append(f"      {reason}: {', '.join(opportunities)}")
    observed = stage.get("observed") or {}
    lines.append(
        "  该时段实测: "
        f"{observed.get('successes', 0)}/{observed.get('known', 0)} 条可判机会成功，"
        f"参照缺失 {observed.get('indeterminate_gap', 0)} 条"
    )
    return "\n".join(lines)


def _format_retirement(item: dict[str, object]) -> str:
    window = item["planned_window"]
    lines = [
        f"退站 {item['work']} / {item['station']}  窗口 {window[0]}–{window[1]}  "
        f"结论: {item['decision']}",
        f"  受影响片区: {', '.join(item['affected_areas'])}",
    ]
    if item["scheduled_window"]:
        lines.append(
            f"  实际排入: 第 {item['scheduled_window'][0]}–{item['scheduled_window'][1]} 天完工"
        )
    for blocker in item["blockers"]:
        lines.append(
            f"  阻断[{blocker['type']}]: {blocker['detail']} "
            f"({', '.join(blocker.get('areas', []))}/{', '.join(blocker.get('strata', []))})"
        )
    if item.get("batch_partners"):
        lines.append(f"  同批其他退站: {', '.join(item['batch_partners'])}")
    batch = item.get("batch_impact") or {}
    if batch:
        lines.append(
            f"  同批退站后可比覆盖: {batch.get('successes', 0)}/{batch.get('known', 0)}，"
            f"状态 {batch.get('status')}"
        )
    for requirement in item["release_requirements"]:
        lines.append(f"  可核准条件: {requirement}")
    for offer in item["next_opportunities"]:
        if offer["known_available"] or offer["indeterminate_available"]:
            lines.append(
                f"  {offer['after_day']} 日后档案中 {offer['area']}/{offer['stratum']} 的机会: "
                f"可判 {', '.join(offer['known_available']) or '无'}；"
                f"参照缺失 {', '.join(offer['indeterminate_available']) or '无'}"
            )
    return "\n".join(lines)


def _format_common_mode(items: list[dict[str, object]]) -> str:
    if not items:
        return "未发现被共同依赖的链。"
    lines = []
    for item in items:
        impact = item["area_impact"]
        lines.append(
            f"链 {item['chain']}: 依赖站 {', '.join(item['relied_stations'])}；"
            f"断链会同时失去 {', '.join(item['dropped_stations'])}"
        )
        if impact:
            for area in impact:
                lines.append(
                    f"  {area['area']}/{area['stratum']}: 成功 "
                    f"{area['before_successes']}/{area['before_known']} → "
                    f"{area['after_successes']}/{area['after_known']} "
                    f"({area['after_known'] and area['after_status']})"
                )
        else:
            lines.append("  对该片区的整片山谷指标无可见影响")
    return "\n".join(lines)


def _format_trials(items: list[dict[str, object]]) -> str:
    lines = []
    for item in items:
        status = "可接受" if item["accepted"] else "不能作为证明"
        lines.append(
            f"回单 {item['report']} ({item['work']}/{item['station']}, 第 {item['day']} 天): {status}"
        )
        lines.append(
            f"  回单记 {item['known_severe']} 条暴雨 / {item['unclassified']} 条参照缺失；"
            f"核对到 {item['verified_known']} / {item['verified_indeterminate']}"
        )
        for problem in item["problems"]:
            lines.append(f"  问题: {problem}")
        if item["next_known_severe_opportunities"]:
            lines.append(
                f"  之后可用的可判暴雨机会: {', '.join(item['next_known_severe_opportunities'])}"
            )
    return "\n".join(lines)


def _format_schedule(items: list[dict[str, object]]) -> str:
    lines = ["施工排程（按实际开工日）:"]
    for item in items:
        lines.append(
            f"  {item['work']} {item['station']} {item['kind']} "
            f"[{item['start']}–{item['end']}] 工位 {item['crew']}"
            + (f" 依赖 {', '.join(item['depends'])}" if item["depends"] else "")
        )
    return "\n".join(lines)


def _format_field(brief: dict[str, object]) -> str:
    lines = [f"现场摘要（截至第 {brief['as_of_stage'][0]}–{brief['as_of_stage'][1]} 天）"]
    for area in brief["areas"]:
        lines.append(f"{area['area']}:")
        for stratum in area["strata"]:
            lines.append(
                f"  {stratum['stratum']} 需要 {stratum['required_independent']} 路独立，"
                f"当前 {stratum['independent']} 路（{'够' if stratum['capable'] else '不足'}），"
                f"站: {', '.join(stratum['stations']) or '无'}"
            )
        for retirement in area["retirements"]:
            blocked = [
                f"{pair[0]}/{pair[1]}" for pair in retirement.get("blocking_areas", [])
            ]
            lines.append(
                f"  退站 {retirement['station']}: {retirement['decision']}，"
                f"阻断 {', '.join(retirement['blockers']) or '无'}"
                + (f"（影响 {', '.join(blocked)}）" if blocked else "")
            )
    for chain in brief.get("common_mode_chains", []):
        lines.append(
            f"共同故障链 {chain['chain']}: 断开会同时失去 "
            f"{', '.join(chain['dropped_stations'])}"
        )
        for impact in chain["area_impact"]:
            lines.append(
                f"  {impact['area']}/{impact['stratum']}: "
                f"{impact['before_successes']}/{impact['before_known']} → "
                f"{impact['after_successes']}/{impact['after_known']}"
            )
    return "\n".join(lines)


def _summary(report) -> str:
    data = report.to_dict()
    comparable = data["comparable"] or {}
    lines = [
        f"可比覆盖（全期参照机会）: {comparable.get('successes', 0)}/{comparable.get('known', 0)} "
        f"= {comparable.get('ratio')}，状态 {comparable.get('status')}，"
        f"证据空白 {comparable.get('indeterminate_gap', 0)} 条",
        "",
        "阶段安排:",
    ]
    for index, stage in enumerate(data["stages"], start=1):
        lines.append(_format_stage(stage, index))
    lines.append("")
    lines.append("退站签认:")
    for item in data["retirements"]:
        lines.append(_format_retirement(item))
    lines.append("")
    lines.append("共同故障:")
    lines.append(_format_common_mode(data["common_mode"]))
    lines.append("")
    lines.append("试运行核对:")
    lines.append(_format_trials(data["trial_findings"]))
    lines.append("")
    lines.append("属性更正到货:")
    for event in data["knowledge_events"]:
        lines.append(
            f"  {event['station']} {event['field']}={event['value']} 实际第 {event['effective_day']} 天生效，"
            f"第 {event['reported_day']} 天才收到（滞后 {event['latency_days']} 天）"
        )
    if data["problems"]:
        lines.append("")
        lines.append("台账问题:")
        for problem in data["problems"]:
            lines.append(f"  {problem}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "location",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="台账目录",
    )
    parser.add_argument(
        "--show",
        choices=[
            "summary",
            "schedule",
            "stages",
            "retirements",
            "field",
            "common-mode",
            "trials",
            "json",
        ],
        default="summary",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="把本次可签认计划写成 JSON 快照，供下一轮复核或现场查阅",
    )
    options = parser.parse_args(argv)
    try:
        ledger = load_ledger(options.location)
    except LedgerError as error:
        print(f"台账无法读取: {error}")
        return 2
    report = build_plan(ledger)
    data = report.to_dict()
    if options.out is not None:
        options.out.parent.mkdir(parents=True, exist_ok=True)
        options.out.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    if options.show == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif options.show == "schedule":
        print(_format_schedule(data["schedule"]))
    elif options.show == "field":
        print(_format_field(data["field_brief"]))
    elif options.show == "stages":
        for index, stage in enumerate(data["stages"], start=1):
            print(_format_stage(stage, index))
    elif options.show == "retirements":
        for item in data["retirements"]:
            print(_format_retirement(item))
    elif options.show == "common-mode":
        print(_format_common_mode(data["common_mode"]))
    elif options.show == "trials":
        print(_format_trials(data["trial_findings"]))
    else:
        print(_summary(report))
    holds = [item for item in data["retirements"] if item["decision"] != "approve"]
    return 0 if not data["problems"] and not holds else 1


if __name__ == "__main__":
    raise SystemExit(main())
