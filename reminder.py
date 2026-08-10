"""生日检测与提醒消息构建。"""

from __future__ import annotations

import datetime
from typing import Any

import astrbot.api.message_components as Comp

from .models import BirthdayRecord

# 特殊 QQ 号，OneBot v11 中代表 @全体成员
AT_ALL = "all"


def find_today_birthdays(
    records: list[BirthdayRecord],
    today: datetime.date | None = None,
    feb29_mode: str = "feb28",
) -> list[BirthdayRecord]:
    """筛选出今天过生日的记录。"""
    today = today or datetime.date.today()
    return [r for r in records if r.is_valid_date() and r.is_birthday_today(today, feb29_mode)]


def upcoming_birthdays(
    records: list[BirthdayRecord],
    count: int,
    today: datetime.date | None = None,
    feb29_mode: str = "feb28",
) -> list[BirthdayRecord]:
    """按距离下一次生日由近到远排序，返回前 count 个。"""
    today = today or datetime.date.today()
    valid = [r for r in records if r.is_valid_date()]
    valid.sort(key=lambda r: (r.next_birthday(today, feb29_mode), r.uin))
    return valid[: max(count, 0)]


def _render_text(text: str, record: BirthdayRecord) -> str:
    """替换模板中的文本类占位符 {uin} {nick} {age}。"""
    age = record.compute_age()
    return (
        text.replace("{uin}", record.uin)
        .replace("{nick}", record.nick)
        .replace("{age}", str(age) if age is not None else "?")
    )


def _tokenize(template: str, record: BirthdayRecord) -> list[tuple[str, str]]:
    """把模板拆分为 [("text", ...)|("at", uin)] 的有序序列。"""
    tokens: list[tuple[str, str]] = []
    for i, part in enumerate(template.split("{at}")):
        if i > 0:
            tokens.append(("at", record.uin))
        if part:
            tokens.append(("text", _render_text(part, record)))
    return tokens


def build_astrbot_chain(template: str, record: BirthdayRecord, at_all: bool) -> list[Any]:
    """构建 AstrBot MessageChain 组件列表（用于 context.send_message）。"""
    chain: list[Any] = []
    if at_all:
        chain.append(Comp.At(qq=AT_ALL))
        chain.append(Comp.Plain("\n"))
    for kind, value in _tokenize(template, record):
        if kind == "at":
            chain.append(Comp.At(qq=value))
        else:
            chain.append(Comp.Plain(value))
    return chain


def build_onebot_message(template: str, record: BirthdayRecord, at_all: bool) -> list[dict]:
    """构建 OneBot v11 消息段数组（用于协议端 send_group_msg）。"""
    message: list[dict] = []
    if at_all:
        message.append({"type": "at", "data": {"qq": AT_ALL}})
        message.append({"type": "text", "data": {"text": "\n"}})
    for kind, value in _tokenize(template, record):
        if kind == "at":
            message.append({"type": "at", "data": {"qq": value}})
        else:
            message.append({"type": "text", "data": {"text": value}})
    return message


def at_all_allowed_by_config(
    enable: bool,
    group_whitelist: list,
    user_whitelist: list,
    group_id: str,
    uin: str,
) -> bool:
    """判断配置层面是否允许对该记录 @全体（不含 bot 管理员校验）。"""
    if not enable:
        return False
    if not group_id:
        return False
    gw = {str(g) for g in group_whitelist}
    uw = {str(u) for u in user_whitelist}
    if gw and str(group_id) not in gw:
        return False
    if uw and str(uin) not in uw:
        return False
    return True
