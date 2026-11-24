"""覆盖、证据与退站签认的行为测试。"""

import csv
import sys
import unittest
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from station_coverage.coverage import (
    batch_retirement_scenario,
    chain_loss_scenario,
    evaluate_comparable,
    evaluate_window,
    max_independent_pairs,
)
from station_coverage.evidence import build_evidence_model
from station_coverage.ledger import load_ledger
from station_coverage.plan import build_plan
from station_coverage.schedule import plan_schedule
from station_coverage.topology import Topology


def _dataset():
    return load_ledger(ROOT / "data")


def _copy_data(directory: Path) -> Path:
    target = directory / "data"
    copytree(ROOT / "data", target)
    return target


def _rewrite(path: Path, rows: list[dict[str, str]]) -> None:
    fields = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _set(target: Path, table: str, match_field: str, match_value: str, field: str, value: str) -> None:
    path = target / f"{table}.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        if row[match_field] == match_value:
            row[field] = value
    _rewrite(path, rows)


def _append(target: Path, table: str, row: dict[str, str]) -> None:
    path = target / f"{table}.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows.append(row)
    _rewrite(path, rows)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.ledger = _dataset()
        self.topology = Topology(self.ledger)
        self.evidence = build_evidence_model(self.ledger)
        self.schedule = plan_schedule(self.ledger)
        for window in list(self.schedule.windows.values()):
            if window.kind == "retire":
                self.schedule.windows.pop(window.work)

    def test_shared_uplink_does_not_count_as_backup(self):
        # O3 与 N1 都挂 U3；V2 暴雨要求 2 路独立，V1 暴雨要求 2 路。
        claims = [
            {"station": "N1", "feed": "F4", "uplink": "U3"},
            {"station": "O3", "feed": "F3", "uplink": "U3"},
        ]
        from station_coverage.coverage import StationClaim

        self.assertEqual(
            max_independent_pairs([StationClaim(**claim) for claim in claims]), 1
        )

    def test_shared_chain_is_reported_as_common_mode(self):
        report = chain_loss_scenario(
            self.ledger,
            self.topology,
            self.evidence,
            self.schedule,
            chain="uplink:U3",
            window_start=9,
            window_end=11,
        )
        self.assertIn("N1", report.valley_summary()["excluded"])
        self.assertIn("O3", report.valley_summary()["excluded"])
        failing = [
            coverage.demand.key
            for coverage in report.failing
            if coverage.demand.area in ("V1", "V2")
        ]
        self.assertIn(("V2", "severe"), failing)

    def test_indeterminate_is_not_a_miss(self):
        report = evaluate_window(
            self.ledger,
            self.topology,
            self.evidence,
            self.schedule,
            window_start=-4,
            window_end=11,
            availability="comparable",
            knowledge_as_of=15,
        )
        coverage = report.demands[("V2", "severe")]
        self.assertEqual(coverage.evidence_gap, len(coverage.indeterminate))
        self.assertEqual(coverage.evidence_gap, 3)
        self.assertIn("K001", [item.opportunity for item in coverage.indeterminate])

    def test_single_opportunity_is_thin_evidence(self):
        report = evaluate_window(
            self.ledger,
            self.topology,
            self.evidence,
            self.schedule,
            window_start=9,
            window_end=11,
            as_of=11,
        )
        coverage = report.demands[("V1", "severe")]
        self.assertEqual(coverage.status, "thin_evidence")
        self.assertFalse(coverage.passes)

    def test_unproven_new_station_is_not_credited(self):
        report = evaluate_comparable(
            self.ledger, self.topology, self.evidence, self.schedule, as_of=15
        )
        # N2 的试运行回单没有参照可判的暴雨机会，不能进入 V2 覆盖。
        coverage = report.demands[("V2", "severe")]
        used = {claim.station for claim in coverage.known[0].claimed}
        self.assertNotIn("N2", used)

    def test_retirement_of_o1_breaks_v2_severe(self):
        report = evaluate_comparable(
            self.ledger,
            self.topology,
            self.evidence,
            self.schedule,
            excluded=("O1",),
            as_of=15,
        )
        self.assertFalse(report.demands[("V2", "severe")].passes)
        reasons = report.demands[("V2", "severe")].by_reason()
        self.assertIn("shared_chain", reasons)

    def test_batch_retirement_is_worse_than_sum_of_parts(self):
        report = batch_retirement_scenario(
            self.ledger,
            self.topology,
            self.evidence,
            self.schedule,
            stations=("O1", "O3"),
            window_start=-4,
            window_end=11,
            as_of=15,
            availability="comparable",
        )
        self.assertFalse(report.provable)
        failing = {coverage.demand.key for coverage in report.failing}
        self.assertIn(("V2", "severe"), failing)


class EvidenceTests(unittest.TestCase):
    def test_trial_report_counts_are_cross_checked(self):
        ledger = _dataset()
        model = build_evidence_model(ledger)
        audit = model.trial_audits["P1"]
        self.assertEqual(audit.known, ["K025", "K033", "K040"])
        self.assertEqual(audit.indeterminate, ["K028", "K036"])
        self.assertEqual(model.acceptance_day("N1"), 9)

    def test_p2_cannot_prove_n2(self):
        ledger = _dataset()
        model = build_evidence_model(ledger)
        audit = model.trial_audits["P2"]
        self.assertEqual(audit.known, [])
        self.assertTrue(audit.problems)
        self.assertIsNone(model.acceptance_day("N2"))

    def test_daily_rain_does_not_stand_in_for_storm(self):
        ledger = _dataset()
        model = build_evidence_model(ledger)
        # N1 只在 V2 的暴雨机会上取得证明；普通降雨也能用，但不能反向。
        self.assertIsNotNone(model.proven_for("N1", "V2", "severe", 15))
        self.assertIsNotNone(model.proven_for("N1", "V2", "normal", 15))
        self.assertIsNone(model.proven_for("N3", "V3", "severe", 14))
        self.assertIsNotNone(model.proven_for("N3", "V3", "severe", 15))


class TopologyTests(unittest.TestCase):
    def test_late_report_is_not_known_before_it_arrives(self):
        ledger = _dataset()
        topology = Topology(ledger)
        self.assertEqual(topology.chain_attributes("N1", 9, known_as_of=8)[1], "U4")
        self.assertEqual(topology.chain_attributes("N1", 9, known_as_of=9)[1], "U3")

    def test_late_arrivals_are_listed_with_latency(self):
        ledger = _dataset()
        topology = Topology(ledger)
        late = {arrival.update.id: arrival for arrival in topology.late_arrivals("N1")}
        self.assertIn("U01", late)
        self.assertEqual(late["U01"].update.reported_day - late["U01"].update.effective_day, 8)


class PlanTests(unittest.TestCase):
    def test_supplied_plan_holds_both_retirements(self):
        report = build_plan(_dataset())
        decisions = {decision.station: decision for decision in report.retirements}
        self.assertEqual(set(decisions), {"O1", "O3"})
        for decision in decisions.values():
            self.assertEqual(decision.decision, "hold")
            self.assertIsNone(decision.scheduled_window)
        types = {blocker["type"] for blocker in decisions["O1"].blockers}
        self.assertIn("common_mode", types)
        self.assertIn("evidence", types)

    def test_o1_alone_would_be_releasable_once_n2_is_proven(self):
        with TemporaryDirectory() as directory:
            target = _copy_data(Path(directory))
            # 给 N2 的试运行回单补一条参照可判的暴雨机会。
            _append(target, "trial_evidence", {"report": "P2", "opportunity": "K025", "reported_day": "12"})
            _set(target, "trial_reports", "report", "P2", "known_severe", "1")
            _set(target, "trial_reports", "report", "P2", "unclassified", "2")
            report = build_plan(load_ledger(target))
        decisions = {decision.station: decision for decision in report.retirements}
        self.assertEqual(decisions["O1"].decision, "approve")
        # 退站完成日不早于 N2 证明送达日；窗口内 O1 仍在运行，覆盖在完工时才失去。
        self.assertGreaterEqual(decisions["O1"].scheduled_window[1], 12)
        # 同批一起退仍会破坏 V2 暴雨，所以 O3 维持保留。
        self.assertEqual(decisions["O3"].decision, "hold")
        self.assertIn(("V2", "severe"), decisions["O3"].blockers[0]["areas"] and [
            (blocker["areas"][0], blocker["strata"][0])
            for blocker in decisions["O3"].blockers
        ])

    def test_resource_window_blocks_retirement(self):
        with TemporaryDirectory() as directory:
            target = _copy_data(Path(directory))
            # W1 最早第 3 天完工，但 W2 的窗口要求第 2 天开工；试运行排不进去，
            # 依赖它的 W3（O1 退站）也就没有可签认的施工时段。
            _set(target, "works", "work", "W2", "begin_by", "2")
            _set(target, "works", "work", "W2", "end_by", "6")
            report = build_plan(load_ledger(target))
        decisions = {decision.station: decision for decision in report.retirements}
        self.assertEqual(decisions["O1"].decision, "hold")
        self.assertIn("resource", {blocker["type"] for blocker in decisions["O1"].blockers})
        self.assertTrue(any("W2" in problem for problem in report.problems))

    def test_new_round_of_data_is_read_without_code_changes(self):
        with TemporaryDirectory() as directory:
            target = _copy_data(Path(directory))
            _append(
                target,
                "opportunities",
                {
                    "opportunity": "K049",
                    "area": "V2",
                    "stratum": "severe",
                    "day": "12",
                    "reference": "known",
                },
            )
            rows = []
            for station in ("O1", "O3", "O4", "N1", "N2", "N3", "REF"):
                rows.append(
                    {
                        "opportunity": "K049",
                        "station": station,
                        "valid_elements": "rain|humidity",
                        "reported_day": "12",
                        "origin": "field_check",
                    }
                )
            for row in rows:
                _append(target, "readings", row)
            report = build_plan(load_ledger(target))
        self.assertTrue(any(stage.end >= 12 for stage in report.stages))
        # 多一条参照可判的机会不会凭空改变 N2 的证据，也不会让 O1 自动获准。
        decisions = {decision.station: decision for decision in report.retirements}
        self.assertEqual(decisions["O1"].decision, "hold")


if __name__ == "__main__":
    unittest.main()
