"""基于 AstrBot 插件 KV 存储的数据访问层。

两种记录模式（per_group / global）的数据完全分开存储，互不影响。
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import BirthdayRecord

# KV 存储键
KEY_RECORDS = "records_{mode}"
KEY_CONFLICTS = "conflicts_{mode}"
KEY_PROCESSED = "processed_announcements"


class KVProvider(Protocol):
    """AstrBot Star 提供的 KV 存储接口（>= 4.9.2）。"""

    async def get_kv_data(self, key: str, default: Any = None) -> Any: ...

    async def put_kv_data(self, key: str, value: Any) -> None: ...

    async def delete_kv_data(self, key: str) -> None: ...


class BirthdayStore:
    """封装生日记录、抓取冲突信息、已处理公告的持久化。"""

    def __init__(self, kv: KVProvider) -> None:
        self._kv = kv

    # ---------- 记录 ----------
    async def get_records(self, mode: str) -> list[BirthdayRecord]:
        raw = await self._kv.get_kv_data(KEY_RECORDS.format(mode=mode), [])
        if not isinstance(raw, list):
            return []
        return [BirthdayRecord.from_dict(d) for d in raw if isinstance(d, dict)]

    async def save_records(self, mode: str, records: list[BirthdayRecord]) -> None:
        await self._kv.put_kv_data(KEY_RECORDS.format(mode=mode), [r.to_dict() for r in records])

    # ---------- 抓取冲突信息（展示在 list 之上）----------
    async def get_conflicts(self, mode: str, group_id: str = "") -> list[str]:
        """获取冲突/跳过信息。

        per_group 模式按 group_id 分桶存储；global 模式存于 group_id="" 桶。
        """
        raw = await self._kv.get_kv_data(KEY_CONFLICTS.format(mode=mode), {})
        if not isinstance(raw, dict):
            return []
        bucket = raw.get(group_id, [])
        if isinstance(bucket, list):
            return [str(x) for x in bucket]
        return []

    async def set_conflicts(self, mode: str, group_id: str, messages: list[str]) -> None:
        raw = await self._kv.get_kv_data(KEY_CONFLICTS.format(mode=mode), {})
        if not isinstance(raw, dict):
            raw = {}
        if messages:
            raw[group_id] = messages
        else:
            raw.pop(group_id, None)
        await self._kv.put_kv_data(KEY_CONFLICTS.format(mode=mode), raw)

    # ---------- 已处理公告去重 ----------
    async def get_processed(self, group_id: str) -> set[str]:
        raw = await self._kv.get_kv_data(KEY_PROCESSED, {})
        if not isinstance(raw, dict):
            return set()
        bucket = raw.get(str(group_id), [])
        if isinstance(bucket, list):
            return {str(x) for x in bucket}
        return set()

    async def add_processed(self, group_id: str, notice_ids: list[str]) -> None:
        if not notice_ids:
            return
        raw = await self._kv.get_kv_data(KEY_PROCESSED, {})
        if not isinstance(raw, dict):
            raw = {}
        gid = str(group_id)
        existing = set(raw.get(gid, []))
        existing.update(str(n) for n in notice_ids)
        # 仅保留最近的一批，避免无限增长
        raw[gid] = list(existing)[-500:]
        await self._kv.put_kv_data(KEY_PROCESSED, raw)
