"""读取并校验山区观测覆盖演练台账。

所有资料都来自 CSV，字段定义见仓库根目录的 ``input_contract.json`` 与
``README.md``。本模块只负责把文本转换成带类型的记录，并在发现基础引用错误时
给出可读的异常；它不做覆盖判定。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "demands": ("area", "stratum", "need", "minimum", "independent"),
    "stations": ("station", "areas", "elements", "feed", "uplink", "kind"),
    "works": ("work", "kind", "station", "begin_by", "end_by", "span", "crew", "depends"),
    "resources": ("crew", "count"),
    "field_updates": ("id", "reported_day", "effective_day", "station", "field", "value"),
    "trial_reports": ("report", "work", "day", "known_severe", "unclassified"),
    "trial_evidence": ("report", "opportunity", "reported_day"),
    "opportunities": ("opportunity", "area", "stratum", "day", "reference"),
    "readings": ("opportunity", "station", "valid_elements", "reported_day", "origin"),
}

KNOWN_REFERENCE = "known"
INDETERMINATE_REFERENCE = "indeterminate"
SEVERE = "severe"
NORMAL = "normal"


class LedgerError(ValueError):
    """台账内的基础引用或字段内容不成立。"""


def _split(value: str) -> tuple[str, ...]:
    """竖线分隔的多值字段；空值表示没有值，而不是空字符串项。"""

    return tuple(item.strip() for item in value.split("|") if item.strip())


def _int(value: str, *, table: str, column: str, line: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise LedgerError(f"{table}.csv 第 {line} 行的 {column} 不是整数：{value!r}") from error


def _float(value: str, *, table: str, column: str, line: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise LedgerError(f"{table}.csv 第 {line} 行的 {column} 不是数字：{value!r}") from error


@dataclass(frozen=True)
class Demand:
    area: str
    stratum: str
    need: tuple[str, ...]
    minimum: float
    independent: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.area, self.stratum)


@dataclass(frozen=True)
class Station:
    station: str
    areas: tuple[str, ...]
    elements: tuple[str, ...]
    feed: str
    uplink: str
    kind: str

    @property
    def is_reference(self) -> bool:
        return self.kind == "reference"

    @property
    def is_new(self) -> bool:
        return self.kind == "new"

    @property
    def is_old(self) -> bool:
        return self.kind == "old"


@dataclass(frozen=True)
class Work:
    work: str
    kind: str
    station: str
    begin_by: int
    end_by: int
    span: int
    crew: str
    depends: tuple[str, ...]

    @property
    def latest_start(self) -> int:
        return self.end_by - self.span


@dataclass(frozen=True)
class Resource:
    crew: str
    count: int


@dataclass(frozen=True)
class FieldUpdate:
    id: str
    reported_day: int
    effective_day: int
    station: str
    field: str
    value: str


@dataclass(frozen=True)
class TrialReport:
    report: str
    work: str
    day: int
    known_severe: int
    unclassified: int


@dataclass(frozen=True)
class TrialEvidence:
    report: str
    opportunity: str
    reported_day: int


@dataclass(frozen=True)
class Opportunity:
    opportunity: str
    area: str
    stratum: str
    day: int
    reference: str

    @property
    def is_known(self) -> bool:
        return self.reference == KNOWN_REFERENCE

    @property
    def is_indeterminate(self) -> bool:
        return self.reference == INDETERMINATE_REFERENCE


@dataclass(frozen=True)
class Reading:
    opportunity: str
    station: str
    valid_elements: tuple[str, ...]
    reported_day: int
    origin: str


@dataclass
class Ledger:
    demands: dict[tuple[str, str], Demand] = field(default_factory=dict)
    stations: dict[str, Station] = field(default_factory=dict)
    works: dict[str, Work] = field(default_factory=dict)
    resources: dict[str, int] = field(default_factory=dict)
    field_updates: list[FieldUpdate] = field(default_factory=list)
    trial_reports: dict[str, TrialReport] = field(default_factory=dict)
    trial_evidence: list[TrialEvidence] = field(default_factory=list)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    readings: dict[tuple[str, str], Reading] = field(default_factory=dict)
    source: Path | None = None

    # -- 便捷查询 ---------------------------------------------------------
    def demands_for_area(self, area: str) -> list[Demand]:
        return [demand for demand in self.demands.values() if demand.area == area]

    def reports_for_work(self, work: str) -> list[TrialReport]:
        return [report for report in self.trial_reports.values() if report.work == work]

    def evidence_for_report(self, report: str) -> list[TrialEvidence]:
        return [evidence for evidence in self.trial_evidence if evidence.report == report]



def audit_references(ledger: Ledger) -> list[str]:
    """返回台账内部的基础引用问题，不抛异常，便于命令行汇总。"""

    issues: list[str] = []
    for report in ledger.trial_reports.values():
        if report.work not in ledger.works:
            issues.append(f"trial_reports.csv 的 {report.report} 指向不存在的工程 {report.work}")
    for evidence in ledger.trial_evidence:
        if evidence.report not in ledger.trial_reports:
            issues.append(f"trial_evidence.csv 的 {evidence.report} 指向不存在的试运行回单")
    for work in ledger.works.values():
        for dependency in work.depends:
            if dependency not in ledger.works:
                issues.append(f"works.csv 的 {work.work} 依赖不存在的工程 {dependency}")
    for update in ledger.field_updates:
        if update.station not in ledger.stations:
            issues.append(f"field_updates.csv 的 {update.id} 指向不存在的站点 {update.station}")
        if update.field not in ("feed", "uplink", "areas", "elements"):
            issues.append(f"field_updates.csv 的 {update.id} 使用了未知属性 {update.field}")
    for opportunity in ledger.opportunities.values():
        if opportunity.reference not in (KNOWN_REFERENCE, INDETERMINATE_REFERENCE):
            issues.append(f"opportunities.csv 的 {opportunity.opportunity} 参照状态 {opportunity.reference!r} 无法解释")
    return issues


def load_ledger(location: str | Path) -> Ledger:
    """读取一个台账目录；表头、字段数、基础引用不成立时抛出 LedgerError。"""

    location = Path(location)
    ledger = Ledger(source=location)
    rows: dict[str, list[tuple[int, dict[str, str]]]] = {}

    for table, expected in TABLE_FIELDS.items():
        path = location / f"{table}.csv"
        if not path.exists():
            raise LedgerError(f"缺少 {path.name}")
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(expected):
                raise LedgerError(f"{path.name} 的表头与交付格式不符")
            table_rows: list[tuple[int, dict[str, str]]] = []
            for line, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise LedgerError(f"{path.name} 第 {line} 行的字段数不符")
                table_rows.append((line, row))
            rows[table] = table_rows

    for line, row in rows["demands"]:
        demand = Demand(
            area=row["area"],
            stratum=row["stratum"],
            need=_split(row["need"]),
            minimum=_float(row["minimum"], table="demands", column="minimum", line=line),
            independent=_int(row["independent"], table="demands", column="independent", line=line),
        )
        if demand.key in ledger.demands:
            raise LedgerError(f"demands.csv 的 {demand.area}/{demand.stratum} 重复")
        ledger.demands[demand.key] = demand

    for line, row in rows["stations"]:
        station = Station(
            station=row["station"],
            areas=_split(row["areas"]),
            elements=_split(row["elements"]),
            feed=row["feed"],
            uplink=row["uplink"],
            kind=row["kind"],
        )
        if station.station in ledger.stations:
            raise LedgerError(f"stations.csv 的 {station.station} 重复")
        ledger.stations[station.station] = station

    for line, row in rows["works"]:
        work = Work(
            work=row["work"],
            kind=row["kind"],
            station=row["station"],
            begin_by=_int(row["begin_by"], table="works", column="begin_by", line=line),
            end_by=_int(row["end_by"], table="works", column="end_by", line=line),
            span=_int(row["span"], table="works", column="span", line=line),
            crew=row["crew"],
            depends=_split(row["depends"]),
        )
        if work.work in ledger.works:
            raise LedgerError(f"works.csv 的 {work.work} 重复")
        if work.kind not in ("install", "trial", "retire"):
            raise LedgerError(f"works.csv 的 {work.work} 使用了未知工程类型 {work.kind!r}")
        if work.station not in ledger.stations:
            raise LedgerError(f"works.csv 的 {work.work} 指向不存在的站点 {work.station}")
        if work.span <= 0 or work.begin_by > work.latest_start:
            raise LedgerError(f"works.csv 的 {work.work} 在给定窗口内无法完成")
        ledger.works[work.work] = work

    for line, row in rows["resources"]:
        resource = Resource(row["crew"], _int(row["count"], table="resources", column="count", line=line))
        if resource.crew in ledger.resources:
            raise LedgerError(f"resources.csv 的 {resource.crew} 重复")
        ledger.resources[resource.crew] = resource.count

    for line, row in rows["field_updates"]:
        update = FieldUpdate(
            id=row["id"],
            reported_day=_int(row["reported_day"], table="field_updates", column="reported_day", line=line),
            effective_day=_int(row["effective_day"], table="field_updates", column="effective_day", line=line),
            station=row["station"],
            field=row["field"],
            value=row["value"],
        )
        ledger.field_updates.append(update)

    for line, row in rows["trial_reports"]:
        report = TrialReport(
            report=row["report"],
            work=row["work"],
            day=_int(row["day"], table="trial_reports", column="day", line=line),
            known_severe=_int(row["known_severe"], table="trial_reports", column="known_severe", line=line),
            unclassified=_int(row["unclassified"], table="trial_reports", column="unclassified", line=line),
        )
        if report.report in ledger.trial_reports:
            raise LedgerError(f"trial_reports.csv 的 {report.report} 重复")
        ledger.trial_reports[report.report] = report

    for line, row in rows["trial_evidence"]:
        ledger.trial_evidence.append(
            TrialEvidence(
                report=row["report"],
                opportunity=row["opportunity"],
                reported_day=_int(row["reported_day"], table="trial_evidence", column="reported_day", line=line),
            )
        )

    for line, row in rows["opportunities"]:
        opportunity = Opportunity(
            opportunity=row["opportunity"],
            area=row["area"],
            stratum=row["stratum"],
            day=_int(row["day"], table="opportunities", column="day", line=line),
            reference=row["reference"],
        )
        if opportunity.opportunity in ledger.opportunities:
            raise LedgerError(f"opportunities.csv 的 {opportunity.opportunity} 重复")
        ledger.opportunities[opportunity.opportunity] = opportunity

    for line, row in rows["readings"]:
        reading = Reading(
            opportunity=row["opportunity"],
            station=row["station"],
            valid_elements=_split(row["valid_elements"]),
            reported_day=_int(row["reported_day"], table="readings", column="reported_day", line=line),
            origin=row["origin"],
        )
        key = (reading.opportunity, reading.station)
        if key in ledger.readings:
            raise LedgerError(f"readings.csv 的 {reading.opportunity}/{reading.station} 重复")
        ledger.readings[key] = reading

    # 基础引用
    for key, reading in ledger.readings.items():
        if reading.opportunity not in ledger.opportunities:
            raise LedgerError(f"readings.csv 的 {reading.opportunity}/{reading.station} 指向不存在的观察机会")
        if reading.station not in ledger.stations:
            raise LedgerError(f"readings.csv 的 {reading.opportunity}/{reading.station} 指向不存在的站点")

    issues = audit_references(ledger)
    if issues:
        raise LedgerError("；".join(issues))
    return ledger
