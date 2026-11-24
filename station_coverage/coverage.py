"""按片区、灾种和观测要求解释覆盖，并把"看得到"与"证得了"分开。

判定规则（与 README 的资料说明一致）：

* 一次机会能否入账，以 ``opportunities`` 中的片区、日期和降水类别为准；
* ``reference=indeterminate`` 的机会既不计成功，也不计漏测，单独列为证据空白；
* 新站只有在与工程关联的试运行回单送达、且在本片区本灾种取得可比降雨证明
  后才算合格观测，日常小雨不能替代暴雨；
* 独立备份要同时区分供电链与回传链：共用其中任意一条的两座站不能互相兜底；
* 退役后的运行覆盖只由仍在运行的站提供，临时参照站不进入覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import EvidenceModel
from .ledger import (
    INDETERMINATE_REFERENCE,
    KNOWN_REFERENCE,
    Demand,
    Ledger,
    Opportunity,
)
from .schedule import Schedule
from .topology import Topology

MINIMUM_SAMPLE = 2
"""一个片区/灾种至少要有的、参照可判的机会数。

低于这个数时，比率只是个别样本的偶然结果，不能据此签认同批退站；报告会把
它标成 ``thin_evidence`` 而不是通过。"""

REASON_NO_STATION = "no_station"
REASON_INSUFFICIENT = "insufficient_station"
REASON_SHARED_CHAIN = "shared_chain"
REASON_UNPROVEN = "unproven"
REASON_BELOW_FLOOR = "below_floor"

REASON_TEXT = {
    REASON_NO_STATION: "该时段没有任何仍在运行且满足要素要求的站",
    REASON_INSUFFICIENT: "合格站数量不足，也没有可补证的候选站",
    REASON_SHARED_CHAIN: "有站上报，但可用站共用供电或回传链，独立备份不足",
    REASON_UNPROVEN: "有候选站，但其有效性尚无可比降雨证明",
    REASON_BELOW_FLOOR: "可判机会的成功比例低于片区下限",
}


@dataclass(frozen=True)
class StationClaim:
    station: str
    feed: str
    uplink: str


@dataclass
class OpportunityOutcome:
    opportunity: str
    day: int
    reference: str
    claimed: list[StationClaim] = field(default_factory=list)
    unproven: list[str] = field(default_factory=list)
    independent: int = 0
    success: bool | None = None
    reason: str | None = None

    @property
    def judged(self) -> bool:
        return self.success is not None


@dataclass
class DemandCoverage:
    demand: Demand
    window_start: int
    window_end: int
    minimum_sample: int = MINIMUM_SAMPLE
    outcomes: list[OpportunityOutcome] = field(default_factory=list)

    @property
    def known(self) -> list[OpportunityOutcome]:
        return [item for item in self.outcomes if item.reference == KNOWN_REFERENCE]

    @property
    def indeterminate(self) -> list[OpportunityOutcome]:
        return [item for item in self.outcomes if item.reference == INDETERMINATE_REFERENCE]

    @property
    def successes(self) -> int:
        return sum(1 for item in self.known if item.success)

    @property
    def ratio(self) -> float | None:
        denominator = len(self.known)
        return self.successes / denominator if denominator else None

    @property
    def sample_adequate(self) -> bool:
        return len(self.known) >= self.minimum_sample

    @property
    def passes(self) -> bool:
        return (
            self.sample_adequate
            and self.ratio is not None
            and self.ratio >= self.demand.minimum
        )

    @property
    def status(self) -> str:
        if not self.known:
            return "no_evidence"
        if not self.sample_adequate:
            return "thin_evidence"
        return "ok" if self.ratio >= self.demand.minimum else "below_floor"

    @property
    def evidence_gap(self) -> int:
        return len(self.indeterminate)

    @property
    def provable_day_range(self) -> tuple[int, int] | None:
        if not self.known:
            return None
        days = [item.day for item in self.known]
        return min(days), max(days)

    def failures(self) -> list[OpportunityOutcome]:
        return [item for item in self.known if not item.success]

    def by_reason(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for item in self.failures():
            grouped.setdefault(item.reason or REASON_BELOW_FLOOR, []).append(item.opportunity)
        return grouped

    def successes_needed(self, extra_sample: int = 0) -> int | None:
        """补入 ``extra_sample`` 条可判机会后，还需要几条成功才能达到下限。"""

        needed = 0
        denominator = len(self.known) + extra_sample
        while (self.successes + needed) / denominator < self.demand.minimum:
            needed += 1
            if needed > extra_sample:
                return None
        return needed

    def summary(self) -> dict[str, object]:
        return {
            "area": self.demand.area,
            "stratum": self.demand.stratum,
            "need": list(self.demand.need),
            "minimum": self.demand.minimum,
            "independent": self.demand.independent,
            "window": [self.window_start, self.window_end],
            "provable_range": list(self.provable_day_range) if self.provable_day_range else None,
            "known": len(self.known),
            "indeterminate_gap": self.evidence_gap,
            "successes": self.successes,
            "ratio": self.ratio,
            "status": self.status,
            "shortfalls": [
                {
                    "opportunity": item.opportunity,
                    "day": item.day,
                    "reason": item.reason,
                    "explanation": REASON_TEXT.get(item.reason or "", ""),
                    "claimed": [
                        {
                            "station": claim.station,
                            "feed": claim.feed,
                            "uplink": claim.uplink,
                        }
                        for claim in item.claimed
                    ],
                    "unproven": list(item.unproven),
                    "independent": item.independent,
                }
                for item in self.failures()
            ],
            "evidence_gaps": [item.opportunity for item in self.indeterminate],
        }


@dataclass
class CoverageReport:
    window_start: int
    window_end: int
    excluded: tuple[str, ...]
    demands: dict[tuple[str, str], DemandCoverage] = field(default_factory=dict)
    kind: str = "window"

    @property
    def failing(self) -> list[DemandCoverage]:
        return [coverage for coverage in self.demands.values() if not coverage.passes]

    @property
    def provable(self) -> bool:
        return bool(self.demands) and all(coverage.passes for coverage in self.demands.values())

    @property
    def no_evidence(self) -> list[DemandCoverage]:
        return [coverage for coverage in self.demands.values() if coverage.status == "no_evidence"]

    @property
    def thin_evidence(self) -> list[DemandCoverage]:
        return [coverage for coverage in self.demands.values() if coverage.status == "thin_evidence"]

    def valley_summary(self) -> dict[str, object]:
        """整片山谷指标：所有片区、所有灾种合并后的可判机会与成功数。"""

        known = sum(len(coverage.known) for coverage in self.demands.values())
        successes = sum(coverage.successes for coverage in self.demands.values())
        gap = sum(coverage.evidence_gap for coverage in self.demands.values())
        failing = [coverage.demand.key for coverage in self.failing]
        return {
            "kind": self.kind,
            "window": [self.window_start, self.window_end],
            "excluded": list(self.excluded),
            "known": known,
            "successes": successes,
            "ratio": successes / known if known else None,
            "indeterminate_gap": gap,
            "failing_strata": failing,
            "no_evidence_strata": [coverage.demand.key for coverage in self.no_evidence],
            "thin_evidence_strata": [coverage.demand.key for coverage in self.thin_evidence],
            "status": "provable" if self.provable else "incomplete",
        }


def max_independent_pairs(claims: list[StationClaim]) -> int:
    """最多的两两不同供电、不同回传的站数（供电链与回传链之间的边不相交匹配）。"""

    if not claims:
        return 0
    feed_adjacency: dict[str, set[str]] = {}
    for claim in claims:
        feed_adjacency.setdefault(claim.feed, set()).add(claim.uplink)
    matched_uplinks: dict[str, str] = {}

    def augment(feed: str, seen: set[str]) -> bool:
        for uplink in sorted(feed_adjacency[feed]):
            if uplink in seen:
                continue
            seen.add(uplink)
            if uplink not in matched_uplinks or augment(matched_uplinks[uplink], seen):
                matched_uplinks[uplink] = feed
                return True
        return False

    count = 0
    for feed in sorted(feed_adjacency):
        if augment(feed, set()):
            count += 1
    return count


def _covers(valid_elements: tuple[str, ...], need: tuple[str, ...]) -> bool:
    return set(need) <= set(valid_elements)


def _station_operates(
    ledger: Ledger,
    evidence: EvidenceModel,
    schedule: Schedule,
    station: str,
    day: int,
    excluded: set[str],
    *,
    availability: str,
) -> bool:
    """``actual`` 只看当天真实在运行的站；``comparable`` 用同一批可比降雨机会
    比较候选网络的成色，因此不按布设日期剔除新站，但仍要求试运行证明。"""

    record = ledger.stations[station]
    if record.is_reference or station in excluded:
        return False
    if availability == "comparable":
        if record.is_new and evidence.acceptance_day(station) is None:
            return False
        return True
    if record.is_old:
        return True
    acceptance = evidence.acceptance_day(station)
    installed = schedule.install_completion(station)
    if installed is None or acceptance is None:
        return False
    return day >= installed and day >= acceptance


def evaluate_window(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    *,
    window_start: int,
    window_end: int,
    excluded: tuple[str, ...] = (),
    as_of: int | None = None,
    require_acceptance: bool = True,
    availability: str = "actual",
    knowledge_as_of: int | None = None,
    kind: str = "window",
) -> CoverageReport:
    """评估一个过渡窗口内的覆盖。

    * ``reference=indeterminate`` 的机会不进入分母，单独计为证据空白；
    * 新站只有在试运行回单送达并取得可比证明后才进入候选；
    * 独立备份按供电与回传两条链同时看；共用其中一条的站不能互相兜底。
    """

    excluded_set = set(excluded)
    as_of = as_of if as_of is not None else _default_as_of(ledger, window_end)
    report = CoverageReport(
        window_start=window_start,
        window_end=window_end,
        excluded=tuple(sorted(excluded_set)),
        kind=kind,
    )
    for demand in ledger.demands.values():
        report.demands[demand.key] = DemandCoverage(
            demand=demand, window_start=window_start, window_end=window_end
        )
    for opportunity in sorted(
        ledger.opportunities.values(), key=lambda item: (item.day, item.opportunity)
    ):
        if not window_start <= opportunity.day <= window_end:
            continue
        demand = ledger.demands.get((opportunity.area, opportunity.stratum))
        if demand is None:
            continue
        outcome = _judge_opportunity(
            ledger,
            topology,
            evidence,
            schedule,
            opportunity,
            demand,
            excluded_set=excluded_set,
            as_of=as_of,
            require_acceptance=require_acceptance,
            availability=availability,
            knowledge_as_of=knowledge_as_of,
        )
        report.demands[demand.key].outcomes.append(outcome)
    return report


def _default_as_of(ledger: Ledger, window_end: int) -> int:
    horizon = max(
        (reading.reported_day for reading in ledger.readings.values()),
        default=window_end,
    )
    return max(horizon, window_end)


def _judge_opportunity(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    opportunity: Opportunity,
    demand: Demand,
    *,
    excluded_set: set[str],
    as_of: int,
    require_acceptance: bool,
    availability: str,
    knowledge_as_of: int | None,
) -> OpportunityOutcome:
    outcome = OpportunityOutcome(
        opportunity=opportunity.opportunity,
        day=opportunity.day,
        reference=opportunity.reference,
    )
    if opportunity.reference == INDETERMINATE_REFERENCE:
        # 参照没看清：既不算漏测也不算成功。
        return outcome
    if opportunity.reference != KNOWN_REFERENCE:
        return outcome

    claims: list[StationClaim] = []
    unproven: list[str] = []
    for station in sorted(ledger.stations):
        if not _station_operates(
            ledger,
            evidence,
            schedule,
            station,
            opportunity.day,
            excluded_set,
            availability=availability,
        ):
            continue
        if not topology.serves(
            station, opportunity.area, opportunity.day, known_as_of=knowledge_as_of
        ):
            continue
        reading = ledger.readings.get((opportunity.opportunity, station))
        if reading is None or reading.reported_day > as_of:
            continue
        if not _covers(reading.valid_elements, demand.need):
            continue
        if not _covers(
            topology.attributes(
                station, opportunity.day, known_as_of=knowledge_as_of
            ).elements,
            demand.need,
        ):
            continue
        record = ledger.stations[station]
        if record.is_new and require_acceptance:
            if evidence.proven_for(
                station, opportunity.area, demand.stratum, as_of
            ) is None:
                unproven.append(station)
                continue
        feed, uplink = topology.chain_attributes(
            station, opportunity.day, known_as_of=knowledge_as_of
        )
        claims.append(StationClaim(station=station, feed=feed, uplink=uplink))

    outcome.claimed = claims
    outcome.unproven = unproven
    outcome.independent = max_independent_pairs(claims)
    if outcome.independent >= demand.independent:
        outcome.success = True
        return outcome

    outcome.success = False
    if not claims:
        outcome.reason = REASON_UNPROVEN if unproven else REASON_NO_STATION
    elif len(claims) >= demand.independent:
        # 站数够但独立链不够：这是共同故障，不是缺站。
        outcome.reason = REASON_SHARED_CHAIN
    elif unproven:
        outcome.reason = REASON_UNPROVEN
    else:
        outcome.reason = REASON_INSUFFICIENT
    return outcome


def chain_loss_scenario(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    *,
    chain: str,
    window_start: int,
    window_end: int,
    excluded: tuple[str, ...] = (),
    as_of: int | None = None,
    availability: str = "actual",
) -> CoverageReport:
    """模拟一条供电或回传链断开：链上的站全部失去观测。

    ``chain`` 形如 ``uplink:U3`` 或 ``feed:F2``。与普通退役不同，这里一次性
    移除同一依赖链上的所有站，用来区分"仅失去一处观测"与"多个站同时消失"。
    """

    if ":" not in chain:
        raise ValueError("链的写法应为 uplink:U3 或 feed:F2")
    mode, target = chain.split(":", 1)
    if mode not in ("feed", "uplink"):
        raise ValueError("链的写法应为 uplink:U3 或 feed:F2")
    dropped = [
        station
        for station in ledger.stations
        if not ledger.stations[station].is_reference
        and _chain_member(topology, station, mode, target, window_end)
    ]
    return evaluate_window(
        ledger,
        topology,
        evidence,
        schedule,
        window_start=window_start,
        window_end=window_end,
        excluded=tuple(set(excluded) | set(dropped)),
        as_of=as_of,
        availability=availability,
        kind=f"chain_loss:{chain}",
    )


def _chain_member(topology: Topology, station: str, mode: str, target: str, day: int) -> bool:
    feed, uplink = topology.chain_attributes(station, day)
    value = uplink if mode == "uplink" else feed
    return value == target


def batch_retirement_scenario(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    *,
    stations: tuple[str, ...],
    window_start: int,
    window_end: int,
    as_of: int | None = None,
    availability: str = "actual",
) -> CoverageReport:
    """同批一起退役后的覆盖，用来发现"逐个审核都通过"的合谋错误。"""

    return evaluate_window(
        ledger,
        topology,
        evidence,
        schedule,
        window_start=window_start,
        window_end=window_end,
        excluded=stations,
        as_of=as_of,
        availability=availability,
        kind="batch_retirement",
    )


def evaluate_comparable(
    ledger: Ledger,
    topology: Topology,
    evidence: EvidenceModel,
    schedule: Schedule,
    *,
    excluded: tuple[str, ...] = (),
    as_of: int | None = None,
    since_day: int | None = None,
    knowledge_as_of: int | None = None,
    kind: str = "comparable",
) -> CoverageReport:
    """用参照可判的机会比较候选网络，不按新站布设日期剔除。

    这是退役可行性的主指标：新旧站必须在同一批可比降雨条件下证明有效性。
    同一片区可能有多次暴雨过程；只在较晚的机会上比较，等于用当时尚未出现的
    天气去否定更早的站，因此默认取全部参照可判机会（``since_day=None``）。
    实际过渡时段的运行覆盖另由 :func:`evaluate_window` 给出。
    """

    horizon_start = (
        since_day
        if since_day is not None
        else min((item.day for item in ledger.opportunities.values()), default=0)
    )
    horizon_end = max((item.day for item in ledger.opportunities.values()), default=0)
    return evaluate_window(
        ledger,
        topology,
        evidence,
        schedule,
        window_start=horizon_start,
        window_end=horizon_end,
        excluded=excluded,
        as_of=as_of,
        availability="comparable",
        knowledge_as_of=knowledge_as_of,
        kind=kind,
    )
