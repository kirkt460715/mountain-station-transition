"""站点属性随施工日和业务收取日变化后的实际依赖链。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .ledger import FieldUpdate, Ledger


@dataclass(frozen=True)
class StationAttributes:
    station: str
    areas: tuple[str, ...]
    elements: tuple[str, ...]
    feed: str
    uplink: str
    kind: str


@dataclass(frozen=True)
class AttributeArrival:
    """一条属性更正实际生效、以及业务收到它的时点。"""

    update: FieldUpdate
    field: str
    value: tuple[str, ...] | str


class Topology:
    """按物理生效日解释站点属性，并可区分"实际如此"与"何时才知道"。"""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._updates: dict[str, list[FieldUpdate]] = {}
        for update in ledger.field_updates:
            if update.station in ledger.stations:
                self._updates.setdefault(update.station, []).append(update)

    def attributes(self, station: str, day: int, *, known_as_of: int | None = None) -> StationAttributes:
        record = self.ledger.stations[station]
        values = {
            "areas": record.areas,
            "elements": record.elements,
            "feed": record.feed,
            "uplink": record.uplink,
        }
        ordered = sorted(self._updates.get(station, []), key=lambda u: (u.effective_day, u.reported_day, u.id))
        for update in ordered:
            if update.effective_day > day:
                continue
            if known_as_of is not None and update.reported_day > known_as_of:
                continue
            if update.field == "areas":
                values["areas"] = tuple(item for item in update.value.split("|") if item)
            elif update.field == "elements":
                values["elements"] = tuple(item for item in update.value.split("|") if item)
            elif update.field == "feed":
                values["feed"] = update.value
            elif update.field == "uplink":
                values["uplink"] = update.value
        return StationAttributes(
            station=station,
            areas=tuple(values["areas"]),
            elements=tuple(values["elements"]),
            feed=str(values["feed"]),
            uplink=str(values["uplink"]),
            kind=record.kind,
        )

    def serves(self, station: str, area: str, day: int, *, known_as_of: int | None = None) -> bool:
        return area in self.attributes(station, day, known_as_of=known_as_of).areas

    def chain_attributes(self, station: str, day: int, *, known_as_of: int | None = None) -> tuple[str, str]:
        attributes = self.attributes(station, day, known_as_of=known_as_of)
        return attributes.feed, attributes.uplink

    def arrivals_for(self, station: str) -> list[AttributeArrival]:
        result: list[AttributeArrival] = []
        for update in sorted(self._updates.get(station, []), key=lambda u: (u.effective_day, u.id)):
            values: tuple[str, ...] | str
            if update.field in ("areas", "elements"):
                values = tuple(item for item in update.value.split("|") if item)
            else:
                values = update.value
            result.append(AttributeArrival(update=update, field=update.field, value=values))
        return result

    def late_arrivals(self, station: str) -> list[AttributeArrival]:
        """实际生效日早于业务收到日的更正；在收到前不能据此签认。"""

        return [
            arrival
            for arrival in self.arrivals_for(station)
            if arrival.update.reported_day > arrival.update.effective_day
        ]

    def shared_chain_groups(self, stations: Iterable[str], day: int) -> dict[str, list[str]]:
        """返回共享供电链或回传链的站群，键为 ``feed:`` 或 ``uplink:`` 前缀。"""

        groups: dict[str, list[str]] = {}
        for station in stations:
            attributes = self.attributes(station, day)
            groups.setdefault(f"feed:{attributes.feed}", []).append(station)
            groups.setdefault(f"uplink:{attributes.uplink}", []).append(station)
        return {chain: members for chain, members in groups.items() if len(members) > 1}

    def affected_areas(self, station: str, day: int, *, known_as_of: int | None = None) -> tuple[str, ...]:
        return self.attributes(station, day, known_as_of=known_as_of).areas
