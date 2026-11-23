"""付款期（YYYY-MM）与自然日的换算、比较与推进。"""
from __future__ import annotations

import calendar
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Period:
    year: int
    month: int

    @classmethod
    def parse(cls, text: str) -> "Period":
        year_s, month_s = text.split("-")
        return cls(int(year_s), int(month_s))

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def end_date(self) -> str:
        last_day = calendar.monthrange(self.year, self.month)[1]
        return f"{self.year:04d}-{self.month:02d}-{last_day:02d}"

    def next(self) -> "Period":
        if self.month == 12:
            return Period(self.year + 1, 1)
        return Period(self.year, self.month + 1)


def first_bookable_period(event_period: Period, locked: set[Period]) -> Period:
    """更正事件发生后第一个未锁定的可入账期。"""
    candidate = event_period
    while candidate in locked:
        candidate = candidate.next()
    return candidate
