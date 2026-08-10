"""数据模型定义。"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BirthdayRecord:
    """一条生日记录。

    Attributes:
        uin: QQ 号（字符串，避免大整数精度问题）。
        nick: 昵称。
        year: 出生年份，<= 0 表示未知。
        month: 出生月份。
        day: 出生日期。
        group_id: 所属群号（per_group 模式使用；私聊/global 模式为空）。
        platform_id: 平台适配器实例 id，用于主动发送提醒。
        umo: unified_msg_origin，作为发送提醒的兜底会话标识。
        source: 来源，"manual"（手动添加）或 "crawl"（公告抓取）。
    """

    uin: str
    nick: str
    year: int
    month: int
    day: int
    group_id: str = ""
    platform_id: str = ""
    umo: str = ""
    source: str = "manual"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BirthdayRecord:
        return cls(
            uin=str(data.get("uin", "")),
            nick=str(data.get("nick", "")),
            year=int(data.get("year", 0) or 0),
            month=int(data.get("month", 0) or 0),
            day=int(data.get("day", 0) or 0),
            group_id=str(data.get("group_id", "")),
            platform_id=str(data.get("platform_id", "")),
            umo=str(data.get("umo", "")),
            source=str(data.get("source", "manual")),
        )

    def is_valid_date(self) -> bool:
        """校验 month/day 是否是合法日期（忽略年份）。"""
        if not (1 <= self.month <= 12):
            return False
        if not (1 <= self.day <= 31):
            return False
        # 用一个闰年来校验 2/29 也合法
        try:
            datetime.date(2000, self.month, self.day)
        except ValueError:
            return False
        return True

    def compute_age(self, today: datetime.date | None = None) -> int | None:
        """根据出生日期计算「今天」的年龄，出生年未知时返回 None。"""
        if self.year <= 0:
            return None
        today = today or datetime.date.today()
        age = today.year - self.year
        # 若今年的生日还没到，年龄减一
        if (today.month, today.day) < (self.month, self.day):
            age -= 1
        return max(age, 0)

    def next_birthday(
        self, today: datetime.date | None = None, feb29_mode: str = "feb28"
    ) -> datetime.date:
        """返回下一次生日的日期（含今天）。用于排序。

        feb29_mode 控制 2/29 生日在平年的顺延方式：feb28 / mar1 / none。
        """
        today = today or datetime.date.today()
        month, day = self.month, self.day
        is_feb29 = month == 2 and day == 29

        def _target(year: int) -> datetime.date | None:
            if is_feb29 and not _is_leap(year):
                if feb29_mode == "feb28":
                    return datetime.date(year, 2, 28)
                if feb29_mode == "mar1":
                    return datetime.date(year, 3, 1)
                return None  # none：平年不触发
            return datetime.date(year, month, day)

        # 最多向后找 8 年，足以覆盖 none 模式下的闰年间隔
        for year in range(today.year, today.year + 9):
            candidate = _target(year)
            if candidate is not None and candidate >= today:
                return candidate
        # 理论上不会走到这里，兜底返回下一个闰年的 2/29
        return datetime.date(today.year + 4, month, day)

    def is_birthday_today(
        self, today: datetime.date | None = None, feb29_mode: str = "feb28"
    ) -> bool:
        """判断今天是否是该记录的生日。

        feb29_mode 控制 2/29 生日在平年的触发方式：feb28 / mar1 / none。
        """
        today = today or datetime.date.today()
        if self.month == today.month and self.day == today.day:
            return True
        # 2/29 生日在平年的顺延触发
        if self.month == 2 and self.day == 29 and not _is_leap(today.year):
            if feb29_mode == "feb28" and today.month == 2 and today.day == 28:
                return True
            if feb29_mode == "mar1" and today.month == 3 and today.day == 1:
                return True
        return False

    def date_str(self) -> str:
        """人类可读的出生日期字符串。"""
        if self.year > 0:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        return f"{self.month:02d}-{self.day:02d}"


@dataclass
class ParsedEntry:
    """从公告中解析出来的一行原始数据。"""

    uin: str
    nick: str
    year: int
    month: int
    day: int
    raw: str = field(default="")


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
