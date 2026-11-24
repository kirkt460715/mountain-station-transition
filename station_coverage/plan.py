"""阶段安排与退站签认：把覆盖、证据、施工资源和共同故障汇总成一份可查计划。

计划分两层：

* **可比覆盖**（``comparable``）用同一批参照可判的机会比较候选网络，是退役
  可行性的依据；新旧站必须在同样的降雨条件下证明有效性。
* **运行覆盖**（各过渡时段）按实际布设和业务收到证明的日期计算，回答"这一段
  时间哪些片区仍有合格观测和独立备份"。

两者都可能存在证据空白：没有参照可判的机会不计成功也不计漏测；样本太薄时
不能签认，只能记为下一轮需要并行或补测的条件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .coverage import (
    REASON_INSUFFICIENT,
    REASON_NO_STATION,
    REASON_SHARED_CHAIN,
    REASON_UNPROVEN,
    CoverageReport,
    DemandCoverage,
    StationClaim,
    evaluate_comparable,
    evaluate_window,
    max_independent_pairs,
)
from .evidence import EvidenceModel, build_evidence_model
from .ledger import SEVERE, Demand, Ledger
from .schedule import Schedule, Window, plan_schedule
from .topology import Topology

BLOCK_RESOURCE = "resource"
BLOCK_COVERAGE = "coverage_floor"
BLOCK_EVIDENCE = "evidence"
BLOCK_COMMON_MODE = "common_mode"

BLOCK_TEXT = {
    BLOCK_RESOURCE: "施工资源或工期窗口排不下",
    BLOCK_COVERAGE: "可判机会的成功比例低于片区下限",
    BLOCK_EVIDENCE: "证据不足：没有可比降雨的证明或样本太薄",
    BLOCK_COMMON_MODE: "共同故障：依赖同一供电或回传链，独立备份不足",
}


@dataclass
class Capability:
    """某片区/灾种在某一天能指望的独立观测。"""

    demand: Demand
    day: int
    claims: list[StationClaim] = field(default_factory=list)
    unproven: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    independent: int = 0
    essential: list[str] = field(default_factory=list)
    shared_chains: dict[str, list[str]] = field(default_factory=dict)

    @property
    def required(self) -> int:
        return self.demand.independent

    @property
    def capable(self) -> bool:
        return self.independent >= self.required

    @property
    def reason(self) -> str | None:
        if self.capable:
            return None
        if self.independent == 0:
            return REASON_UNPROVEN if self.unproven else REASON_NO_STATION
        return REASON_SHARED_CHAIN

    def summary(self) -> dict[str, object]:
        return {
            "area": self.demand.area,
            "stratum": self.demand.stratum,
            "need": list(self.demand.need),
            "required_independent": self.required,
            "independent": self.independent,
            "capable": self.capable,
            "reason": self.reason,
            "stations": [
                {"station": claim.station, "feed": claim.feed, "uplink": claim.uplink}
                for claim in self.claims
            ],
            "unproven": list(self.unproven),
            "rejected": dict(self.rejected),
            "essential": list(self.essential),
            "shared_chains": {
                chain: list(members) for chain, members in self.shared_chains.items()
            },
        }


@dataclass
class Stage:
    start: int
    end: int
    active: list[str]
    credited: list[str]
    retirements_completed: list[str]
    capability: dict[tuple[str, str], Capability] = field(default_factory=dict)
    observed: CoverageReport | None = None
    blockers: list[dict[str, object]] = field(default_factory=list)
    caveats: list[dict[str, object]] = field(default_factory=list)
    status: str = "unknown"

    @property
    def window(self) -> tuple[int, int]:
        return (self.start, self.end)

    def summary(self) -> dict[str, object]:
        observed = self.observed.valley_summary() if self.observed else None
        return {
            "window": [self.start, self.end],
            "active": list(self.active),
            "credited": list(self.credited),
            "retirements_completed": list(self.retirements_completed),
            "status": self.status,
            "capability": [
                self.capability[key].summary() for key in sorted(self.capability)
            ],
            "observed": observed,
            "observed_details": [
                self.observed.demands[key].summary()
                for key in sorted(self.observed.demands)
            ]
            if self.observed
            else [],
            "blockers": self.blockers,
            "caveats": self.caveats,
        }


@dataclass
class RetirementDecision:
    work: str
    station: str
    affected_areas: tuple[str, ...]
    planned_start: int
    planned_completion: int
    scheduled_window: tuple[int, int] | None = None
    decision: str = "hold"
    blockers: list[dict[str, object]] = field(default_factory=list)
    batch_partners: list[str] = field(default_factory=list)
    batch_impact: dict[str, object] | None = None
    comparable_if_retired: dict[str, object] | None = None
    comparable_if_retained: dict[str, object] | None = None
    post_window: tuple[int, int] | None = None
    operational_if_retired: dict[str, object] | None = None
    operational_if_retained: dict[str, object] | None = None
    release_requirements: list[str] = field(default_factory=list)
    next_opportunities: list[dict[str, object]] = field(default_factory=list)
    batch_impact_with_assumed_evidence: dict[str, object] | None = None

    def summary(self) -> dict[str, object]:
        return {
            "work": self.work,
            "station": self.station,
            "affected_areas": list(self.affected_areas),
            "planned_window": [self.planned_start, self.planned_completion],
            "scheduled_window": list(self.scheduled_window) if self.scheduled_window else None,
            "decision": self.decision,
            "blockers": self.blockers,
            "batch_partners": list(self.batch_partners),
            "batch_impact": self.batch_impact,
            "comparable_if_retired": self.comparable_if_retired,
            "comparable_if_retained": self.comparable_if_retained,
            "post_window": list(self.post_window) if self.post_window else None,
            "operational_if_retired": self.operational_if_retired,
            "operational_if_retained": self.operational_if_retained,
            "release_requirements": list(self.release_requirements),
            "next_opportunities": self.next_opportunities,
            "batch_impact_with_assumed_evidence": self.batch_impact_with_assumed_evidence,
        }


@dataclass
class PlanReport:
    ledger: Ledger
    topology: Topology
    evidence: EvidenceModel
    schedule: Schedule
    stages: list[Stage] = field(default_factory=list)
    retirements: list[RetirementDecision] = field(default_factory=list)
    knowledge_events: list[dict[str, object]] = field(default_factory=list)
    common_mode: list[dict[str, object]] = field(default_factory=list)
    trial_findings: list[dict[str, object]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    horizon: int = 0
    comparable: CoverageReport | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "horizon": self.horizon,
            "schedule": [
                {
                    "work": window.work,
                    "station": window.station,
                    "kind": window.kind,
                    "crew": window.crew,
                    "start": window.start,
                    "end": window.end,
                    "depends": list(window.depends),
                }
                for window in sorted(
                    self.schedule.windows.values(), key=lambda item: (item.start, item.work)
                )
            ],
            "comparable": self.comparable.valley_summary() if self.comparable else None,
            "comparable_details": [
                self.comparable.demands[key].summary()
                for key in sorted(self.comparable.demands)
            ]
            if self.comparable
            else [],
            "stages": [stage.summary() for stage in self.stages],
            "retirements": [decision.summary() for decision in self.retirements],
            "knowledge_events": self.knowledge_events,
            "common_mode": self.common_mode,
            "trial_findings": self.trial_findings,
            "field_brief": field_brief(self),
            "problems": list(self.problems),
        }


# ---------------------------------------------------------------------------
# 站点状态与能力
# ---------------------------------------------------------------------------


def _retired_by(schedule: Schedule, station: str, day: int) -> bool:
    completion = schedule.retire_completion(station)
    return completion is not None and day >= completion


def _station_view(
    ledger: Ledger, schedule: Schedule, evidence: EvidenceModel, station: str, day: int
) -> tuple[bool, bool]:
    """返回 (在运行, 可计入覆盖)。"""

    record = ledger.stations[station]
    if record.is_reference:
        return False, False
    if _retired_by(schedule, station, day):
        return False, False
    if record.is_new:
        installed = schedule.install_completion(station)
        if installed is None or day < installed:
            return False, False
        acceptance = evidence.acceptance_day(station)
        return True, acceptance is not None and day >= acceptance
    return True, True


def _operating(ledger: Ledger, schedule: Schedule, station: str, day: int) -> bool:
    record = ledger.stations[station]
    if record.is_reference:
        return False
    if _retired_by(schedule, station, day):
        return False
    if record.is_new:
        installed = schedule.install_completion(station)
        return installed is not None and day >= installed
    return True


def _active_stations(ledger: Ledger, schedule: Schedule, day: int) -> list[str]:
    return sorted(
        station
        for station in ledger.stations
        if _operating(ledger, schedule, station, day)
    )


def _credited_stations(
    ledger: Ledger, schedule: Schedule, evidence: EvidenceModel, day: int
) -> list[str]:
    return sorted(
        station
        for station in ledger.stations
        if _station_view(ledger, schedule, evidence, station, day)[1]
    )


def _shared_chains(claims: list[StationClaim]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for claim in claims:
        groups.setdefault(f"feed:{claim.feed}", []).append(claim.station)
        groups.setdefault(f"uplink:{claim.uplink}", []).append(claim.station)
    return {
        chain: sorted(set(members))
        for chain, members in groups.items()
        if len(set(members)) > 1
    }


def _capability(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    demand: Demand,
    day: int,
    *,
    excluded: set[str] | None = None,
    require_acceptance: bool = True,
) -> Capability:
    excluded = excluded or set()
    capability = Capability(demand=demand, day=day)
    for station in sorted(ledger.stations):
        record = ledger.stations[station]
        if record.is_reference or station in excluded:
            continue
        if _retired_by(schedule, station, day):
            continue
        if record.is_new:
            installed = schedule.install_completion(station)
            if installed is None or day < installed:
                continue
        if demand.area not in topology.affected_areas(station, day):
            continue
        if not set(demand.need) <= set(topology.attributes(station, day).elements):
            capability.rejected[station] = "缺少所需观测要素"
            continue
        if record.is_new and require_acceptance:
            acceptance = evidence.acceptance_day(station)
            if acceptance is None or day < acceptance:
                capability.unproven.append(station)
                capability.rejected[station] = "试运行尚未取得可比降雨证明"
                continue
        feed, uplink = topology.chain_attributes(station, day)
        capability.claims.append(StationClaim(station=station, feed=feed, uplink=uplink))
    capability.independent = max_independent_pairs(capability.claims)
    capability.shared_chains = _shared_chains(capability.claims)
    if capability.capable:
        for claim in capability.claims:
            remaining = [item for item in capability.claims if item.station != claim.station]
            if max_independent_pairs(remaining) < capability.required:
                capability.essential.append(claim.station)
    return capability


# ---------------------------------------------------------------------------
# 施工排程：先排安装与试运行，再按覆盖可行性安排退站
# ---------------------------------------------------------------------------


def _place_retirements(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
) -> dict[str, dict[str, object]]:
    """按依赖关系和覆盖可行性把退站工程排进各自窗口；排不进就保留原站。"""

    outcome: dict[str, dict[str, object]] = {}
    placement_outcome = outcome
    retire_works = sorted(
        (work for work in ledger.works.values() if work.kind == "retire"),
        key=lambda item: (item.latest_start, item.work),
    )
    placed_retired: list[str] = []
    crew_intervals: dict[str, list[tuple[int, int]]] = {}
    for window in schedule.windows.values():
        crew_intervals.setdefault(window.crew, []).append((window.start, window.end))

    for work in retire_works:
        dependency_end = 0
        blockers: list[dict[str, object]] = []
        for dependency in work.depends:
            dependency_window = schedule.windows.get(dependency)
            if dependency_window is None:
                if dependency in placement_outcome:
                    blockers.append(
                        {
                            "type": BLOCK_RESOURCE,
                            "detail": f"前置工程 {dependency} 未获核准，不能开工",
                        }
                    )
                else:
                    blockers.append(
                        {
                            "type": BLOCK_RESOURCE,
                            "detail": f"前置工程 {dependency} 未排入窗口",
                        }
                    )
                dependency_end = None
            elif dependency_end is not None:
                dependency_end = max(dependency_end, dependency_window.end)
        if dependency_end is None:
            outcome[work.work] = {"window": None, "blockers": blockers}
            continue
        earliest = dependency_end

        earliest = dependency_end
        latest = work.latest_start
        feasible_day: int | None = None
        chosen_start: int | None = None
        for start in range(earliest, latest + 1):
            completion = start + work.span
            intervals = crew_intervals.get(work.crew, [])
            if not all(
                completion <= left or start >= right for left, right in intervals
            ):
                continue
            if _retirement_supported(
                ledger, topology, evidence, schedule, work.station, completion,
                prior_retired=tuple(placed_retired),
            ):
                feasible_day = completion
                chosen_start = start
                break

        if chosen_start is None:
            blockers.extend(
                _retirement_blockers(
                    ledger, topology, evidence, schedule, work.station,
                    max(work.begin_by + work.span, earliest),
                    areas=None, prior_retired=tuple(placed_retired),
                )
            )
            outcome[work.work] = {"window": None, "blockers": blockers}
            continue

        window = Window(
            work=work.work,
            station=work.station,
            kind=work.kind,
            crew=work.crew,
            start=chosen_start,
            end=chosen_start + work.span,
            depends=work.depends,
        )
        schedule.windows[work.work] = window
        crew_intervals.setdefault(work.crew, []).append((window.start, window.end))
        placed_retired.append(work.station)
        outcome[work.work] = {
            "window": window,
            "blockers": [],
            "earliest_supported": feasible_day,
        }
    return outcome


def _retirement_supported(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    station: str,
    completion: int,
    *,
    prior_retired: tuple[str, ...] = (),
) -> bool:
    areas = set(topology.affected_areas(station, completion))
    if not areas:
        return True
    excluded = tuple(set(prior_retired) | {station})
    coverage = evaluate_comparable(
        ledger, topology, evidence, schedule, excluded=excluded, as_of=completion
    )
    for key, demand_coverage in coverage.demands.items():
        if key[0] in areas and not demand_coverage.passes:
            return False
    return True


def _retirement_blockers(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    station: str,
    completion: int,
    *,
    areas: set[str] | None,
    prior_retired: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    affected = areas or set(topology.affected_areas(station, completion))
    excluded = tuple(set(prior_retired) | {station})
    coverage = evaluate_comparable(
        ledger, topology, evidence, schedule, excluded=excluded, as_of=completion
    )
    blockers: list[dict[str, object]] = []
    for key, demand_coverage in sorted(coverage.demands.items()):
        if key[0] not in affected or demand_coverage.passes:
            continue
        demand = demand_coverage.demand
        grouped = demand_coverage.by_reason()
        shared = _replacement_shared_chains(
            ledger, topology, evidence, schedule, demand, completion, excluded
        )
        unproven = _unproven_replacements(
            ledger, topology, evidence, schedule, demand, completion, excluded
        )
        if REASON_SHARED_CHAIN in grouped or shared:
            blockers.append(
                {
                    "type": BLOCK_COMMON_MODE,
                    "detail": BLOCK_TEXT[BLOCK_COMMON_MODE],
                    "areas": [demand.area],
                    "strata": [demand.stratum],
                    "chains": shared,
                    "opportunities": grouped.get(REASON_SHARED_CHAIN, []),
                }
            )
        if unproven or REASON_UNPROVEN in grouped:
            blockers.append(
                {
                    "type": BLOCK_EVIDENCE,
                    "detail": BLOCK_TEXT[BLOCK_EVIDENCE],
                    "areas": [demand.area],
                    "strata": [demand.stratum],
                    "stations": unproven,
                    "opportunities": grouped.get(REASON_UNPROVEN, []),
                }
            )
        if REASON_NO_STATION in grouped or REASON_INSUFFICIENT in grouped:
            blockers.append(
                {
                    "type": BLOCK_COVERAGE,
                    "detail": "退站后该片区合格观测不足",
                    "areas": [demand.area],
                    "strata": [demand.stratum],
                    "opportunities": grouped.get(REASON_NO_STATION, [])
                    + grouped.get(REASON_INSUFFICIENT, []),
                }
            )
        if (
            demand_coverage.status == "below_floor"
            and not blockers
        ):
            blockers.append(
                {
                    "type": BLOCK_COVERAGE,
                    "detail": BLOCK_TEXT[BLOCK_COVERAGE],
                    "areas": [demand.area],
                    "strata": [demand.stratum],
                    "successes": demand_coverage.successes,
                    "known": len(demand_coverage.known),
                    "ratio": demand_coverage.ratio,
                    "minimum": demand.minimum,
                    "reasons": grouped,
                }
            )
        elif demand_coverage.status in ("no_evidence", "thin_evidence"):
            blockers.append(
                {
                    "type": BLOCK_EVIDENCE,
                    "detail": "退站后的可比机会样本不足",
                    "areas": [demand.area],
                    "strata": [demand.stratum],
                    "known": len(demand_coverage.known),
                    "evidence_gaps": [
                        gap.opportunity for gap in demand_coverage.indeterminate
                    ],
                }
            )
    return blockers


def _replacement_capability(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    demand: Demand,
    day: int,
    excluded: tuple[str, ...],
) -> Capability:
    return _capability(
        ledger, topology, evidence, schedule, demand, day, excluded=set(excluded)
    )


def _replacement_shared_chains(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    demand: Demand,
    day: int,
    excluded: tuple[str, ...],
) -> dict[str, list[str]]:
    capability = _replacement_capability(
        ledger, topology, evidence, schedule, demand, day, excluded
    )
    return capability.shared_chains


def _unproven_replacements(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    demand: Demand,
    day: int,
    excluded: tuple[str, ...],
) -> list[str]:
    capability = _replacement_capability(
        ledger, topology, evidence, schedule, demand, day, excluded
    )
    usable = {
        claim.station for claim in capability.claims
    }
    return [
        station
        for station in capability.unproven
        if demand.area in topology.affected_areas(station, day)
        and set(demand.need) <= set(topology.attributes(station, day).elements)
        and station not in usable
    ]


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------


def _signature(
    ledger: Ledger,
    topology: Topology,
    schedule: Schedule,
    evidence: EvidenceModel,
    day: int,
) -> tuple:
    parts = []
    for station in sorted(ledger.stations):
        record = ledger.stations[station]
        if record.is_reference:
            continue
        operating, credited = _station_view(ledger, schedule, evidence, station, day)
        if not operating:
            continue
        attributes = topology.attributes(station, day)
        parts.append(
            (station, credited, attributes.areas, attributes.elements, attributes.feed, attributes.uplink)
        )
    return tuple(parts)


def _stage_windows(
    ledger: Ledger,
    topology: Topology,
    schedule: Schedule,
    evidence: EvidenceModel,
    horizon: int,
) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    current: tuple | None = None
    start = 0
    for day in range(0, horizon + 1):
        signature = _signature(ledger, topology, schedule, evidence, day)
        if current is None or signature == current:
            current = signature
            continue
        windows.append((start, day - 1))
        current = signature
        start = day
    if current is not None:
        windows.append((start, horizon))
    return windows


def _stage_blockers(capability: dict[tuple[str, str], Capability]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for key, item in sorted(capability.items()):
        if item.capable:
            continue
        if item.reason == REASON_SHARED_CHAIN or item.shared_chains:
            blockers.append(
                {
                    "type": BLOCK_COMMON_MODE,
                    "detail": BLOCK_TEXT[BLOCK_COMMON_MODE],
                    "areas": [key[0]],
                    "strata": [key[1]],
                    "chains": item.shared_chains,
                    "independent": item.independent,
                    "required": item.required,
                }
            )
        if item.unproven:
            blockers.append(
                {
                    "type": BLOCK_EVIDENCE,
                    "detail": BLOCK_TEXT[BLOCK_EVIDENCE],
                    "areas": [key[0]],
                    "strata": [key[1]],
                    "stations": list(item.unproven),
                }
            )
        if item.reason in (REASON_NO_STATION, REASON_INSUFFICIENT):
            blockers.append(
                {
                    "type": BLOCK_COVERAGE,
                    "detail": "该片区/灾种的合格观测站不足",
                    "areas": [key[0]],
                    "strata": [key[1]],
                }
            )
    return blockers


def _stage_caveats(
    coverage: CoverageReport, capability: dict[tuple[str, str], Capability]
) -> list[dict[str, object]]:
    """只记录真正影响可证范围的空白；结构上能覆盖但样本薄的时段单独提示。"""

    caveats: list[dict[str, object]] = []
    thin: list[tuple[str, str]] = []
    for demand_key, item in sorted(coverage.demands.items()):
        if item.evidence_gap:
            caveats.append(
                {
                    "type": BLOCK_EVIDENCE,
                    "detail": "该时段有参照缺失的机会，未计入漏测或成功",
                    "areas": [demand_key[0]],
                    "strata": [demand_key[1]],
                    "evidence_gaps": [gap.opportunity for gap in item.indeterminate],
                }
            )
        if item.status in ("no_evidence", "thin_evidence"):
            thin.append(demand_key)
        elif item.status == "below_floor":
            caveats.append(
                {
                    "type": BLOCK_COVERAGE,
                    "detail": "该时段实测成功比例低于片区下限",
                    "areas": [demand_key[0]],
                    "strata": [demand_key[1]],
                    "successes": item.successes,
                    "known": len(item.known),
                    "ratio": item.ratio,
                    "minimum": item.demand.minimum,
                    "reasons": item.by_reason(),
                }
            )
    if thin:
        caveats.append(
            {
                "type": BLOCK_EVIDENCE,
                "detail": "该时段可判机会样本过薄，不能单独作为验收依据；仅用于运行查询",
                "areas": sorted({key[0] for key in thin}),
                "strata": sorted({key[1] for key in thin}),
                "pairs": [{"area": key[0], "stratum": key[1]} for key in thin],
            }
        )
    return caveats


def _analyse_stage(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    start: int,
    end: int,
) -> Stage:
    active = _active_stations(ledger, schedule, end)
    credited = _credited_stations(ledger, schedule, evidence, end)
    completed = sorted(
        station
        for station in ledger.stations
        if not ledger.stations[station].is_reference
        and schedule.retire_completion(station) is not None
        and schedule.retire_completion(station) <= end
    )
    stage = Stage(
        start=start,
        end=end,
        active=active,
        credited=credited,
        retirements_completed=completed,
    )
    for demand in ledger.demands.values():
        stage.capability[demand.key] = _capability(
            ledger, topology, evidence, schedule, demand, end
        )
    stage.observed = evaluate_window(
        ledger,
        topology,
        evidence,
        schedule,
        window_start=start,
        window_end=end,
        as_of=end,
        kind="stage",
    )
    stage.blockers = _stage_blockers(stage.capability)
    stage.caveats = _stage_caveats(stage.observed, stage.capability)
    if any(blocker["type"] == BLOCK_COMMON_MODE for blocker in stage.blockers):
        stage.status = "blocked_common_mode"
    elif any(blocker["type"] == BLOCK_COVERAGE for blocker in stage.blockers):
        stage.status = "blocked_coverage"
    elif stage.blockers:
        stage.status = "evidence_pending"
    elif any(
        caveat.get("type") == BLOCK_COVERAGE for caveat in stage.caveats
    ):
        stage.status = "observed_shortfall"
    elif stage.caveats:
        stage.status = "operational_only"
    else:
        stage.status = "provable"
    return stage


# ---------------------------------------------------------------------------
# 证据到货、试运行发现、共同故障
# ---------------------------------------------------------------------------


def _knowledge_events(
    ledger: Ledger, topology: Topology, retirements: list[RetirementDecision]
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for station in ledger.stations:
        for arrival in topology.late_arrivals(station):
            update = arrival.update
            events.append(
                {
                    "station": station,
                    "field": update.field,
                    "value": update.value,
                    "effective_day": update.effective_day,
                    "reported_day": update.reported_day,
                    "latency_days": update.reported_day - update.effective_day,
                    "affected_retirements": [
                        decision.work
                        for decision in retirements
                        if decision.station == station
                    ],
                    "note": "实际生效早于业务收到；此前按旧值作出的安排需复核",
                }
            )
    return events


def _trial_findings(ledger: Ledger, evidence: EvidenceModel) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for report_id, audit in sorted(evidence.trial_audits.items()):
        report = audit.report
        station = audit.station
        accepted = evidence.acceptance_day(station) == report.day
        next_gap = _next_known_opportunities(
            ledger, station, report.day, stratum=SEVERE
        )
        findings.append(
            {
                "report": report_id,
                "work": report.work,
                "station": station,
                "day": report.day,
                "known_severe": report.known_severe,
                "unclassified": report.unclassified,
                "verified_known": len(audit.known),
                "verified_indeterminate": len(audit.indeterminate),
                "accepted": accepted,
                "accepted_day": evidence.acceptance_day(station),
                "problems": list(audit.problems),
                "proven_areas": sorted(
                    {
                        proof.area
                        for key, proofs in evidence.proofs.items()
                        if key[0] == station
                        for proof in proofs
                    }
                ),
                "next_known_severe_opportunities": next_gap,
                "evidence_gaps": list(audit.indeterminate),
            }
        )
    return findings


def _next_known_opportunities(
    ledger: Ledger, station: str, after_day: int, *, stratum: str | None = None
) -> list[str]:
    station_areas = set(ledger.stations[station].areas)
    return [
        opportunity.opportunity
        for opportunity in sorted(
            ledger.opportunities.values(), key=lambda item: (item.day, item.opportunity)
        )
        if opportunity.day > after_day
        and opportunity.area in station_areas
        and opportunity.is_known
        and (stratum is None or opportunity.stratum == stratum)
    ]


def _common_mode_findings(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    horizon: int,
) -> list[dict[str, object]]:
    relied: set[str] = set()
    for demand in ledger.demands.values():
        capability = _capability(
            ledger, topology, evidence, schedule, demand, horizon
        )
        relied |= {claim.station for claim in capability.claims}
    if not relied:
        return []
    base = evaluate_comparable(
        ledger, topology, evidence, schedule, as_of=horizon, kind="comparable"
    )
    findings: list[dict[str, object]] = []
    for chain, members in sorted(topology.shared_chain_groups(sorted(relied), horizon).items()):
        mode, target = chain.split(":", 1)
        dropped = sorted(
            station
            for station in ledger.stations
            if not ledger.stations[station].is_reference
            and (
                topology.chain_attributes(station, horizon)[1]
                if mode == "uplink"
                else topology.chain_attributes(station, horizon)[0]
            )
            == target
        )
        loss = evaluate_comparable(
            ledger,
            topology,
            evidence,
            schedule,
            excluded=tuple(dropped),
            as_of=horizon,
            kind=f"chain_loss:{chain}",
        )
        findings.append(
            {
                "chain": chain,
                "relied_stations": sorted(members),
                "dropped_stations": dropped,
                "base": base.valley_summary(),
                "loss": loss.valley_summary(),
                "area_impact": _area_impact(base, loss),
            }
        )
    return findings


def _area_impact(base: CoverageReport, loss: CoverageReport) -> list[dict[str, object]]:
    impact = []
    for key in sorted(base.demands):
        before = base.demands[key]
        after = loss.demands.get(key)
        if after is None:
            continue
        if before.successes == after.successes and len(before.known) == len(after.known):
            continue
        impact.append(
            {
                "area": key[0],
                "stratum": key[1],
                "before_successes": before.successes,
                "before_known": len(before.known),
                "after_successes": after.successes,
                "after_known": len(after.known),
                "after_status": after.status,
            }
        )
    return impact


# ---------------------------------------------------------------------------
# 退站决策
# ---------------------------------------------------------------------------


def _release_requirements(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    work: WorkProxy,
    completion: int,
    areas: set[str],
) -> tuple[list[str], list[dict[str, object]]]:
    """退站未获准时，说明补什么才可能核准；等待本身不算证据。"""

    requirements: list[str] = []
    next_offers: list[dict[str, object]] = []
    excluded = (work.station,)
    for demand in ledger.demands.values():
        if demand.area not in areas:
            continue
        coverage = _coverage_with_assumed_proof(
            ledger, topology, evidence, schedule, demand, work.station, completion, excluded
        )
        targets = _next_proof_targets(
            ledger, topology, evidence, schedule, demand, completion, excluded
        )
        capable_now = _capability(
            ledger, topology, evidence, schedule, demand, completion, excluded=set(excluded)
        )
        slots_needed = max(0, demand.independent - capable_now.independent)
        if coverage.passes:
            next_offers.append(
                {
                    "area": demand.area,
                    "stratum": demand.stratum,
                    "after_day": completion,
                    "known_available": _known_after(ledger, demand, completion),
                    "indeterminate_available": _indeterminate_after(
                        ledger, demand, completion
                    ),
                    "note": "退站后可比覆盖仍达标，无需补测",
                }
            )
            continue
        for target in targets:
            if target["would_pass"]:
                requirements.append(
                    f"{target['station']} 需在 {demand.area}/{demand.stratum} 取得至少一条"
                    f"参照可判、要素满足 {'|'.join(demand.need)} 的暴雨过程，"
                    f"并送达可比试运行回单后才可对该片区核准退站"
                )
        if not any(target["would_pass"] for target in targets):
            requirements.append(
                f"{demand.area}/{demand.stratum} 仅补证明仍不够：退站后独立观测还差 "
                f"{slots_needed} 路，需保留现站或先分离共用供电/回传链"
            )
        if capable_now.shared_chains:
            for chain, members in capable_now.shared_chains.items():
                requirements.append(
                    f"{demand.area}/{demand.stratum} 的 {'、'.join(members)} 共用 {chain}，"
                    "断链会同时失去；需改挂独立链或拆开批次"
                )
        next_offers.append(
            {
                "area": demand.area,
                "stratum": demand.stratum,
                "after_day": completion,
                "known_available": _known_after(ledger, demand, completion),
                "indeterminate_available": _indeterminate_after(
                    ledger, demand, completion
                ),
                "note": _waiting_note(ledger, demand, completion),
            }
        )
    deduped = list(dict.fromkeys(requirements))
    return deduped, next_offers


def _known_after(ledger: Ledger, demand: Demand, day: int) -> list[str]:
    return [
        item.opportunity
        for item in sorted(
            ledger.opportunities.values(), key=lambda row: (row.day, row.opportunity)
        )
        if item.area == demand.area
        and item.stratum == demand.stratum
        and item.day >= day
        and item.is_known
    ]


def _indeterminate_after(ledger: Ledger, demand: Demand, day: int) -> list[str]:
    return [
        item.opportunity
        for item in sorted(
            ledger.opportunities.values(), key=lambda row: (row.day, row.opportunity)
        )
        if item.area == demand.area
        and item.stratum == demand.stratum
        and item.day >= day
        and item.is_indeterminate
    ]


def _waiting_note(ledger: Ledger, demand: Demand, day: int) -> str:
    known = _known_after(ledger, demand, day)
    gaps = _indeterminate_after(ledger, demand, day)
    if not known and not gaps:
        return "档案中没有该片区/灾种在退站日之后的观察机会，等待补不出证据"
    if not known and gaps:
        return (
            "档案中只有参照缺失的机会，无法据此判断成败；需延长并行期并补测暴雨"
        )
    return "档案中的后续机会尚未观测，不能预先算作通过"


def _batch_impact(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    stations: tuple[str, ...],
    completion: int,
) -> dict[str, object]:
    batch = tuple(sorted(stations))
    coverage = evaluate_comparable(
        ledger,
        topology,
        evidence,
        schedule,
        excluded=batch,
        as_of=completion,
        kind="batch",
    )
    return coverage.valley_summary()


def _coverage_with_assumed_proof(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    demand: Demand,
    station: str,
    day: int,
    excluded: tuple[str, ...],
) -> DemandCoverage:
    """假设某站在 ``day`` 补到证明后，该片区/灾种的可比覆盖。"""

    probe_evidence = evidence.with_assumed_proof(
        station, demand.area, demand.stratum, day
    )
    probe = evaluate_comparable(
        ledger,
        topology,
        probe_evidence,
        schedule,
        excluded=excluded,
        as_of=day,
        kind="probe",
    )
    return probe.demands[demand.key]


def _next_proof_targets(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    demand: Demand,
    day: int,
    excluded: tuple[str, ...],
) -> list[dict[str, object]]:
    """列出补哪几个站的证明可能解锁该片区；不包含"等几场雨"的承诺。"""

    capability = _replacement_capability(
        ledger, topology, evidence, schedule, demand, day, excluded
    )
    targets = []
    for station in capability.unproven:
        if demand.area not in topology.affected_areas(station, day):
            continue
        if not set(demand.need) <= set(topology.attributes(station, day).elements):
            continue
        coverage = _coverage_with_assumed_proof(
            ledger, topology, evidence, schedule, demand, station, day, excluded
        )
        targets.append(
            {
                "station": station,
                "would_pass": coverage.passes,
                "would_status": coverage.status,
                "would_ratio": coverage.ratio,
                "would_successes": coverage.successes,
                "would_known": len(coverage.known),
            }
        )
    return targets


def _retirement_decision(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    work: WorkProxy,
    placement: dict[str, object],
    horizon: int,
    batch: tuple[str, ...],
) -> RetirementDecision:
    station = work.station
    scheduled = placement.get("window")
    completion = (
        scheduled.end
        if scheduled is not None
        else max(work.begin_by + work.span, 0)
    )
    affected = set(topology.affected_areas(station, completion))
    areas = tuple(sorted(affected))
    decision = RetirementDecision(
        work=work.work,
        station=station,
        affected_areas=areas,
        planned_start=work.begin_by,
        planned_completion=work.end_by,
        scheduled_window=(scheduled.start, scheduled.end) if scheduled else None,
        batch_partners=[item for item in batch if item != station],
        post_window=(completion, max(completion, horizon)),
    )
    decision.blockers = list(placement.get("blockers") or [])
    if scheduled is None and placement.get("window") is None and not decision.blockers:
        decision.blockers.append(
            {
                "type": BLOCK_RESOURCE,
                "detail": "在给定的 begin_by/end_by 窗口内排不下该退站",
            }
        )

    decision.decision = "approve" if scheduled is not None else "hold"
    post_start, post_end = decision.post_window
    decision.operational_if_retired = evaluate_window(
        ledger, topology, evidence, schedule,
        window_start=post_start, window_end=post_end, as_of=post_end,
        excluded=(station,), kind="retirement",
    ).valley_summary()
    decision.operational_if_retained = evaluate_window(
        ledger, topology, evidence, schedule,
        window_start=post_start, window_end=post_end, as_of=post_end,
        excluded=(), kind="retained",
    ).valley_summary()

    decision.comparable_if_retired = evaluate_comparable(
        ledger, topology, evidence, schedule,
        excluded=(station,), as_of=completion, kind="comparable",
    ).valley_summary()
    decision.comparable_if_retained = evaluate_comparable(
        ledger, topology, evidence, schedule,
        as_of=completion, kind="comparable",
    ).valley_summary()
    decision.batch_impact = _batch_impact(
        ledger, topology, evidence, schedule, batch, completion
    )
    requirements, next_offers = _release_requirements(
        ledger, topology, evidence, schedule, work, completion, affected
    )
    decision.release_requirements = requirements
    decision.next_opportunities = next_offers
    decision.batch_impact_with_assumed_evidence = _batch_impact_with_proofs(
        ledger, topology, evidence, schedule, batch, completion
    )
    return decision


def _batch_impact_with_proofs(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    stations: tuple[str, ...],
    completion: int,
) -> dict[str, object] | None:
    """同批退站若先补齐待证站，是否还能达标。只说明条件，不代表已通过。"""

    probed = evidence
    changed = False
    for station in ledger.stations:
        record = ledger.stations[station]
        if not record.is_new or evidence.acceptance_day(station) is not None:
            continue
        for demand in ledger.demands.values():
            if demand.area not in record.areas:
                continue
            if evidence.proof_records(station, demand.area, demand.stratum):
                continue
            probed = probed.with_assumed_proof(
                station, demand.area, demand.stratum, completion
            )
            changed = True
    if not changed:
        return None
    coverage = evaluate_comparable(
        ledger, topology, probed, schedule,
        excluded=tuple(sorted(stations)), as_of=completion, kind="batch_conditional",
    )
    summary = coverage.valley_summary()
    summary["assumption"] = "假设所有待证新站均补齐证明；不是当前已满足的条件"
    return summary


class WorkProxy:
    """计划内部使用的工程视图，保留 CSV 中的窗口与工期。"""

    def __init__(self, work) -> None:
        self.work = work.work
        self.station = work.station
        self.begin_by = work.begin_by
        self.end_by = work.end_by
        self.span = work.span
        self.crew = work.crew
        self.depends = work.depends
        self.latest_start = work.latest_start


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _horizon(ledger: Ledger, schedule: Schedule, evidence: EvidenceModel) -> int:
    values = [0]
    if ledger.opportunities:
        values.append(max(item.day for item in ledger.opportunities.values()))
    if schedule.windows:
        values.append(max(window.end for window in schedule.windows.values()))
    if evidence.acceptances:
        values.append(max(evidence.acceptances.values()))
    return max(values)


def build_plan(ledger: Ledger) -> PlanReport:
    topology = Topology(ledger)
    evidence = build_evidence_model(ledger)
    schedule = plan_schedule(ledger, skip_kinds=("retire",))
    provisional = _horizon(ledger, schedule, evidence)
    placement = _place_retirements(ledger, topology, evidence, schedule)
    horizon = max(
        provisional,
        max(
            (entry["window"].end for entry in placement.values() if entry.get("window")),
            default=0,
        ),
    )

    report = PlanReport(
        ledger=ledger,
        topology=topology,
        evidence=evidence,
        schedule=schedule,
        horizon=horizon,
    )
    report.problems = list(evidence.problems) + list(schedule.problems)
    report.comparable = evaluate_comparable(
        ledger, topology, evidence, schedule, as_of=horizon, kind="comparable"
    )
    for start, end in _stage_windows(ledger, topology, schedule, evidence, horizon):
        report.stages.append(
            _analyse_stage(ledger, topology, evidence, schedule, start, end)
        )

    retire_works = sorted(
        (work for work in ledger.works.values() if work.kind == "retire"),
        key=lambda item: (schedule.windows.get(item.work).start if item.work in schedule.windows else item.latest_start, item.work),
    )
    batch = tuple(sorted(work.station for work in retire_works))
    for work in retire_works:
        report.retirements.append(
            _retirement_decision(
                ledger,
                topology,
                evidence,
                schedule,
                WorkProxy(work),
                placement.get(work.work, {"window": None, "blockers": []}),
                horizon,
                batch,
            )
        )
    report.knowledge_events = _knowledge_events(ledger, topology, report.retirements)
    report.trial_findings = _trial_findings(ledger, evidence)
    report.common_mode = _common_mode_findings(
        ledger, topology, evidence, schedule, horizon
    )
    return report


def field_brief(report: PlanReport) -> dict[str, object]:
    """现场可读摘要：每个片区当前由谁提供观测、适用于哪些退站结论。"""

    ledger = report.ledger
    final = report.stages[-1] if report.stages else None
    areas: dict[str, dict[str, object]] = {}
    for demand in ledger.demands.values():
        entry = areas.setdefault(
            demand.area,
            {"area": demand.area, "strata": [], "retirements": []},
        )
        capability = final.capability.get(demand.key) if final else None
        entry["strata"].append(
            {
                "stratum": demand.stratum,
                "need": list(demand.need),
                "minimum": demand.minimum,
                "required_independent": demand.independent,
                "stations": [claim.station for claim in capability.claims]
                if capability
                else [],
                "independent": capability.independent if capability else 0,
                "capable": capability.capable if capability else False,
            }
        )
    for decision in report.retirements:
        for area in decision.affected_areas:
            entry = areas.setdefault(
                area, {"area": area, "strata": [], "retirements": []}
            )
            entry["retirements"].append(
                {
                    "station": decision.station,
                    "decision": decision.decision,
                    "work": decision.work,
                    "blockers": sorted(
                        {blocker["type"] for blocker in decision.blockers}
                    ),
                    "releasable_when": list(decision.release_requirements),
                    "blocking_areas": sorted(
                        {
                            (blocker["areas"][0], blocker["strata"][0])
                            for blocker in decision.blockers
                            if blocker.get("areas") and blocker.get("strata")
                        }
                    ),
                }
            )
    supervisory = [
        {
            "chain": item["chain"],
            "dropped_stations": item["dropped_stations"],
            "area_impact": item["area_impact"],
        }
        for item in report.common_mode
    ]
    return {
        "as_of_stage": [final.start, final.end] if final else None,
        "areas": [areas[key] for key in sorted(areas)],
        "retirements": [decision.summary() for decision in report.retirements],
        "common_mode_chains": supervisory,
    }
