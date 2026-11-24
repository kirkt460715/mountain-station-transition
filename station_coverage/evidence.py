"""把试运行回单与现场机会转换成"哪些站、在哪些片区、哪种降雨条件下已被证明"。

关键约束：

* 只有 ``reference=known`` 的机会能作为证明；参照缺失的机会既不能算漏测，
  也不能算成功（见 README）。
* 普通降雨机会不能证明暴雨能力，但一次被独立参照确认的暴雨过程可以同时
  作为普通降雨的比较证据。
* 新站的证明只来自与本工程试运行回单关联的 ``trial_evidence`` 行，不能拿
  同一站在其他时段零散回传的记录充当验收依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import (
    INDETERMINATE_REFERENCE,
    KNOWN_REFERENCE,
    NORMAL,
    SEVERE,
    Ledger,
    TrialReport,
)


@dataclass(frozen=True, order=True)
class Proof:
    station: str
    area: str
    stratum: str
    opportunity: str
    day: int
    reported_day: int


@dataclass
class TrialAudit:
    report: TrialReport
    station: str
    evidence: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    indeterminate: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


STATION_ACCEPTANCE_MIN_SEVERE = 1


@dataclass
class EvidenceModel:
    proofs: dict[tuple[str, str, str], list[Proof]] = field(default_factory=dict)
    trial_audits: dict[str, TrialAudit] = field(default_factory=dict)
    acceptances: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def accepted_station(self, station: str, as_of: int) -> bool:
        """新站是否已有一份含独立参照暴雨机会的试运行回单送达。"""

        day = self.acceptances.get(station)
        return day is not None and day <= as_of

    def acceptance_day(self, station: str) -> int | None:
        return self.acceptances.get(station)

    def proof_records(self, station: str, area: str, stratum: str) -> list[Proof]:
        return list(self.proofs.get((station, area, stratum), []))

    def proven_for(self, station: str, area: str, stratum: str, as_of: int) -> Proof | None:
        """返回 as_of 之前已送达、可用于该片区/灾种的最早证明。

        暴雨证明可以下探到普通降雨；普通降雨证明不能上探到暴雨。
        """

        accepted = [
            proof
            for proof in self.proof_records(station, area, stratum)
            if proof.reported_day <= as_of
        ]
        if accepted:
            return min(accepted, key=lambda item: (item.reported_day, item.opportunity))
        if stratum == NORMAL:
            severe = [
                proof
                for proof in self.proof_records(station, area, SEVERE)
                if proof.reported_day <= as_of
            ]
            if severe:
                return min(severe, key=lambda item: (item.reported_day, item.opportunity))
        return None

    def proof_day(self, station: str, area: str, stratum: str) -> int | None:
        """证明最早可用的业务日；仅用于安排退役的最早可签认时点。"""

        best: int | None = None
        for proof in self.proof_records(station, area, stratum):
            best = proof.reported_day if best is None else min(best, proof.reported_day)
        if best is None and stratum == NORMAL:
            for proof in self.proof_records(station, area, SEVERE):
                best = proof.reported_day if best is None else min(best, proof.reported_day)
        return best

    def add_proof(self, proof: Proof) -> None:
        key = (proof.station, proof.area, proof.stratum)
        records = self.proofs.setdefault(key, [])
        if proof not in records:
            records.append(proof)

    def with_assumed_proof(
        self, station: str, area: str, stratum: str, day: int
    ) -> "EvidenceModel":
        """返回一份副本，假设该站在 ``day`` 补到一条参照可判的证明。

        只用于"补哪条证明能解锁"的推演，不写回原始证据。
        """

        clone = EvidenceModel(
            proofs={key: list(value) for key, value in self.proofs.items()},
            trial_audits=dict(self.trial_audits),
            acceptances=dict(self.acceptances),
            problems=list(self.problems),
        )
        clone.add_proof(
            Proof(
                station=station,
                area=area,
                stratum=stratum,
                opportunity=f"假设-{station}-{area}-{stratum}",
                day=day,
                reported_day=day,
            )
        )
        previous = clone.acceptances.get(station)
        clone.acceptances[station] = day if previous is None else min(previous, day)
        return clone


def _stratum_compatible(proof_stratum: str, demand_stratum: str) -> bool:
    """证明的降雨类别是否可比。暴雨可覆盖普通，反向不成立。"""

    if proof_stratum == demand_stratum:
        return True
    return proof_stratum == SEVERE and demand_stratum == NORMAL


def _covers(reading_elements: tuple[str, ...], need: tuple[str, ...]) -> bool:
    return set(need) <= set(reading_elements)


def build_evidence_model(ledger: Ledger) -> EvidenceModel:
    model = EvidenceModel()
    trial_work_station = {
        work.work: ledger.stations[work.station].station
        for work in ledger.works.values()
        if work.kind == "trial"
    }

    for report in ledger.trial_reports.values():
        station = trial_work_station.get(report.work)
        audit = TrialAudit(report=report, station=station or "")
        model.trial_audits[report.report] = audit
        if station is None:
            audit.problems.append(f"试运行回单 {report.report} 找不到对应站")
            continue
        station_record = ledger.stations[station]
        evidence_rows = ledger.evidence_for_report(report.report)
        for evidence in evidence_rows:
            audit.evidence.append(evidence.opportunity)
            opportunity = ledger.opportunities.get(evidence.opportunity)
            if opportunity is None:
                audit.problems.append(f"回单 {report.report} 关联了不存在的观察机会 {evidence.opportunity}")
                continue
            if opportunity.area not in station_record.areas:
                audit.problems.append(
                    f"回单 {report.report} 关联的 {opportunity.opportunity} 不在 {station} 的负责片区"
                )
                continue
            reading = ledger.readings.get((opportunity.opportunity, station))
            if reading is None:
                audit.problems.append(
                    f"回单 {report.report} 关联的 {opportunity.opportunity} 缺少 {station} 的读数"
                )
                continue
            if opportunity.reference == INDETERMINATE_REFERENCE:
                audit.indeterminate.append(opportunity.opportunity)
                continue
            if opportunity.reference != KNOWN_REFERENCE:
                audit.problems.append(
                    f"回单 {report.report} 关联的 {opportunity.opportunity} 参照状态无法解释"
                )
                continue
            audit.known.append(opportunity.opportunity)
            # 同一片区、同一降雨类别，且该站在机会中的有效要素满足需求，
            # 才算一条可用证明。需求从 demands 表中取；一个站可能覆盖多片区。
            for demand in ledger.demands_for_area(opportunity.area):
                if not _stratum_compatible(opportunity.stratum, demand.stratum):
                    continue
                if not _covers(reading.valid_elements, demand.need):
                    continue
                model.add_proof(
                    Proof(
                        station=station,
                        area=opportunity.area,
                        stratum=demand.stratum,
                        opportunity=opportunity.opportunity,
                        day=opportunity.day,
                        reported_day=max(report.day, evidence.reported_day),
                    )
                )
        if len(audit.known) != report.known_severe:
            audit.problems.append(
                f"回单 {report.report} 记录 known_severe={report.known_severe}，"
                f"但关联机会中可判为暴雨的只有 {len(audit.known)} 条"
            )
        if len(audit.indeterminate) != report.unclassified:
            audit.problems.append(
                f"回单 {report.report} 记录 unclassified={report.unclassified}，"
                f"但关联机会中参照缺失的有 {len(audit.indeterminate)} 条"
            )
        if not audit.known:
            audit.problems.append(
                f"回单 {report.report} 没有任何参照可判的暴雨机会，不能作为该站的有效性证明"
            )
        elif report.known_severe >= STATION_ACCEPTANCE_MIN_SEVERE:
            previous = model.acceptances.get(station)
            model.acceptances[station] = report.day if previous is None else min(previous, report.day)
        model.problems.extend(audit.problems)
    return model
