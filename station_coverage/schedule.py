"""施工窗口与工位资源下的阶段排程。

工程可以顺延，但不能超出 ``begin_by``/``end_by``；退役完工后不可回滚。
同一工位（``crew``）每天可同时开工的数量取自 ``resources``。本模块只负责给出
可排入的时段结构，覆盖可行性由 ``plan`` 在排程时另行判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import Ledger, Work


@dataclass(frozen=True)
class Window:
    work: str
    station: str
    kind: str
    crew: str
    start: int
    end: int
    depends: tuple[str, ...]

    @property
    def completion_day(self) -> int:
        """完工日按不使用该工位的首日计算；与 ``end_by`` 同日可退。"""

        return self.end


@dataclass
class Schedule:
    windows: dict[str, Window] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    horizon: int = 0

    def window(self, work: str) -> Window:
        return self.windows[work]

    def start_day(self, work: str) -> int:
        return self.windows[work].start

    def completion(self, work: str) -> int:
        return self.windows[work].completion_day

    def works_of(self, station: str, kind: str | None = None) -> list[Window]:
        return [
            window
            for window in self.windows.values()
            if window.station == station and (kind is None or window.kind == kind)
        ]

    def install_completion(self, station: str) -> int | None:
        candidates = [window.end for window in self.works_of(station, "install")]
        return min(candidates) if candidates else None

    def retire_completion(self, station: str) -> int | None:
        candidates = [window.end for window in self.works_of(station, "retire")]
        return min(candidates) if candidates else None

    def trial_completion(self, station: str) -> int | None:
        candidates = [window.end for window in self.works_of(station, "trial")]
        return min(candidates) if candidates else None


def capacity_allows(intervals: list[tuple[int, int]], count: int) -> bool:
    """在满负荷的一小段日内检查是否超过工位数。"""

    for day in range(0, max((end for _, end in intervals), default=0) + 1):
        used = sum(1 for start, end in intervals if start <= day < end)
        if used > count:
            return False
    return True


def _slot(
    ledger: Ledger, work: Work, earliest: int, intervals: list[tuple[int, int]]
) -> int | None:
    """找最早的开工日，使工期落在窗口内，且工位占用不超限。"""

    capacity = ledger.resources.get(work.crew, 0)
    if capacity <= 0:
        return None
    latest = work.end_by - work.span
    for start in range(max(earliest, work.begin_by, 0), latest + 1):
        candidate = intervals + [(start, start + work.span)]
        if capacity_allows(candidate, capacity):
            return start
    return None


def plan_schedule(ledger: Ledger, *, skip_kinds: tuple[str, ...] = ()) -> Schedule:
    """按依赖关系和工位容量排程。

    同一工位的工程可以并行，但总数不超过 ``resources`` 中的工位数；吊装与
    试运行共享相应工位。``skip_kinds`` 用于先排安装/试运行，再由调用方安排
    需要覆盖判定的退站工程。
    """

    schedule = Schedule(horizon=max((work.end_by for work in ledger.works.values()), default=0))
    pending = [
        work for work in ledger.works.values() if work.kind not in skip_kinds
    ]
    placed: dict[str, Window] = {}
    crew_intervals: dict[str, list[tuple[int, int]]] = {}

    while pending:
        progress = False
        for work in sorted(
            pending, key=lambda item: (item.begin_by, item.latest_start, item.work)
        ):
            ready = 0
            dependencies_ready = True
            for dependency in work.depends:
                if dependency not in placed:
                    if dependency in {item.work for item in pending}:
                        dependencies_ready = False
                        break
                    schedule.problems.append(
                        f"{work.work} 的前置工程 {dependency} 不在可排程范围内"
                    )
                    dependencies_ready = False
                    break
                ready = max(ready, placed[dependency].end)
            if not dependencies_ready:
                continue
            intervals = crew_intervals.get(work.crew, [])
            start = _slot(ledger, work, ready, intervals)
            if start is None:
                schedule.problems.append(
                    f"{work.work} 在 {work.begin_by}..{work.end_by} 内找不到可用的 "
                    f"{work.crew} 工位"
                )
                pending.remove(work)
                progress = True
                continue
            window = Window(
                work=work.work,
                station=work.station,
                kind=work.kind,
                crew=work.crew,
                start=start,
                end=start + work.span,
                depends=work.depends,
            )
            placed[work.work] = window
            crew_intervals.setdefault(work.crew, []).append((start, start + work.span))
            pending.remove(work)
            progress = True
        if not progress:
            for work in pending:
                schedule.problems.append(f"{work.work} 的前置工程无法排入窗口")
            break

    schedule.windows = placed
    schedule.horizon = max(
        [work.end_by for work in ledger.works.values()]
        + [window.end for window in placed.values()]
        + [0]
    )
    return schedule
