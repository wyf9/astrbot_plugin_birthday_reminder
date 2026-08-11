"""群公告抓取与内容解析。"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from astrbot.api import logger

from .models import ParsedEntry


def _truncate(text: str, limit: int = 500) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"...(共 {len(text)} 字)"


def parse_announcement(
    text: str, parse_mode: str, regex: str
) -> tuple[list[ParsedEntry], list[str]]:
    """解析公告文本。

    Returns:
        (entries, errors)：成功解析的条目与错误信息列表。
    """
    logger.debug(f"[birthday] 解析公告：parse_mode={parse_mode!r} 原文={_truncate(text)!r}")
    if parse_mode == "regex":
        entries, errors = _parse_regex(text, regex)
    else:
        entries, errors = _parse_csv(text)
    logger.debug(
        f"[birthday] 解析结果：命中 {len(entries)} 条，错误 {len(errors)} 条 -> "
        f"entries={[(e.uin, e.nick, e.year, e.month, e.day) for e in entries]} errors={errors}"
    )
    return entries, errors


def _to_int(value: str | None) -> int:
    if value is None:
        return 0
    value = value.strip()
    return int(value) if value.isdigit() else 0


def _parse_csv(text: str) -> tuple[list[ParsedEntry], list[str]]:
    entries: list[ParsedEntry] = []
    errors: list[str] = []
    reader = csv.reader(io.StringIO(text))
    for lineno, row in enumerate(reader, start=1):
        # 去除空白单元格与整行空白
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        # 跳过表头（首列不是纯数字 QQ 号）
        if not cells[0].isdigit():
            continue
        if len(cells) < 5:
            errors.append(f"第 {lineno} 行字段不足(需要 5 列): {','.join(cells)}")
            continue
        uin, nick, year_s, month_s, day_s = cells[0], cells[1], cells[2], cells[3], cells[4]
        month, day = _to_int(month_s), _to_int(day_s)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            errors.append(f"第 {lineno} 行月份/日期非法: {','.join(cells)}")
            continue
        entries.append(
            ParsedEntry(
                uin=uin,
                nick=nick or uin,
                year=_to_int(year_s),
                month=month,
                day=day,
                raw=",".join(cells),
            )
        )
    return entries, errors


def _parse_regex(text: str, regex: str) -> tuple[list[ParsedEntry], list[str]]:
    entries: list[ParsedEntry] = []
    errors: list[str] = []
    try:
        pattern = re.compile(regex)
    except re.error as e:
        return [], [f"正则表达式编译失败: {e}"]

    for match in pattern.finditer(text):
        groups = match.groupdict()
        uin = (groups.get("uin") or "").strip()
        if not uin.isdigit():
            continue
        month = _to_int(groups.get("month"))
        day = _to_int(groups.get("day"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            errors.append(f"匹配项月份/日期非法: {match.group(0)!r}")
            continue
        nick = (groups.get("nick") or "").strip() or uin
        entries.append(
            ParsedEntry(
                uin=uin,
                nick=nick,
                year=_to_int(groups.get("year")),
                month=month,
                day=day,
                raw=match.group(0),
            )
        )
    return entries, errors


async def fetch_group_notices(client: Any, group_id: str) -> list[dict[str, Any]]:
    """调用协议端 API 获取群公告列表。

    返回形如 [{"notice_id": str, "text": str}, ...]。
    兼容 Napcat / Lagrange 的 `_get_group_notice` 返回结构。
    """
    logger.debug(f"[birthday] 调用协议端 _get_group_notice group_id={group_id}")
    try:
        ret = await client.api.call_action("_get_group_notice", group_id=int(group_id))
    except Exception as e:  # noqa: BLE001 - 网络/协议端错误不应导致插件崩溃
        logger.warning(f"[birthday] 获取群 {group_id} 公告失败: {e}")
        return []

    logger.debug(
        f"[birthday] 群 {group_id} 公告原始返回 type={type(ret).__name__} "
        f"value={_truncate(repr(ret), 1000)}"
    )
    notices = ret if isinstance(ret, list) else ret.get("data", []) if isinstance(ret, dict) else []
    logger.debug(f"[birthday] 群 {group_id} 解析出 {len(notices or [])} 条公告条目")
    result: list[dict[str, Any]] = []
    for idx, item in enumerate(notices or []):
        if not isinstance(item, dict):
            logger.debug(f"[birthday] 群 {group_id} 公告[{idx}] 非 dict，跳过: {item!r}")
            continue
        notice_id = str(item.get("notice_id") or item.get("id") or "")
        text = _extract_notice_text(item)
        logger.debug(
            f"[birthday] 群 {group_id} 公告[{idx}] keys={list(item.keys())} "
            f"notice_id={notice_id!r} text={_truncate(text)!r}"
        )
        if notice_id and text:
            result.append({"notice_id": notice_id, "text": text})
        else:
            logger.debug(
                f"[birthday] 群 {group_id} 公告[{idx}] 缺少 notice_id 或 text，"
                f"未纳入处理（若 text 为空说明字段结构未适配）"
            )
    logger.debug(f"[birthday] 群 {group_id} 最终纳入处理的公告 {len(result)} 条")
    return result


def _extract_notice_text(item: dict[str, Any]) -> str:
    """从公告条目里提取纯文本内容，兼容多种字段结构。"""
    message = item.get("message")
    # Napcat: message 是 dict，含 text 字段
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str):
            return text
    # 有的实现 message 是 list[{"text": ...}]
    if isinstance(message, list):
        parts = [str(seg.get("text", "")) for seg in message if isinstance(seg, dict)]
        joined = "".join(parts)
        if joined:
            return joined
    # 兜底字段
    for key in ("text", "content"):
        val = item.get(key)
        if isinstance(val, str) and val:
            return val
    return ""
