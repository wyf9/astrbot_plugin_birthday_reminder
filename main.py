"""生日提醒插件主入口。

提供 /birthday add | del | list | crawl | test | trigger 指令，
支持手动录入与从群公告抓取生日信息，并在生日当天自动发送祝福。
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .crawler import fetch_group_notices, parse_announcement
from .models import BirthdayRecord
from .reminder import (
    at_all_allowed_by_config,
    build_astrbot_chain,
    build_onebot_message,
    find_today_birthdays,
    upcoming_birthdays,
)
from .storage import BirthdayStore

# 事件模式下，同一群公告重复触发的最小间隔（秒），用于去抖
_NOTICE_DEBOUNCE_SECONDS = 15


@register(
    "astrbot_plugin_birthday_reminder",
    "wyf9",
    "记录群成员生日并在生日当天自动发送祝福，支持手动添加 / 从群公告抓取。",
    "v1.0.0",
    "https://github.com/wyf9/astrbot_plugin_birthday_reminder",
)
class BirthdayReminder(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.store = BirthdayStore(self)
        self._scheduler: Any = None
        self._self_id_cache: dict[int, str] = {}
        self._notice_last_crawl: dict[str, float] = {}
        self._notice_hooked = False

    # ================= 生命周期 =================
    async def initialize(self) -> None:
        self._setup_scheduler()

    async def terminate(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[birthday] 关闭调度器失败: {e}")
            self._scheduler = None

    def _setup_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except Exception as e:  # noqa: BLE001
            logger.error(f"[birthday] 无法导入 apscheduler，定时功能不可用: {e}")
            return

        hour, minute = self._parse_check_time()
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._job_birthday_check,
            CronTrigger(hour=hour, minute=minute),
            id="birthday_daily_check",
            replace_existing=True,
        )

        crawl = self._crawl_cfg()
        if crawl["trigger_mode"] in ("poll", "both"):
            interval = max(int(crawl["poll_interval_minutes"]), 1)
            self._scheduler.add_job(
                self._job_poll_crawl,
                IntervalTrigger(minutes=interval),
                id="birthday_poll_crawl",
                replace_existing=True,
            )
        self._scheduler.start()
        logger.info(
            f"[birthday] 调度器已启动：每日 {hour:02d}:{minute:02d} 检测生日；"
            f"抓取触发模式={crawl['trigger_mode']}"
        )

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        """AstrBot 加载完成后，按需注册协议端群公告/通知事件监听。"""
        crawl = self._crawl_cfg()
        if crawl["trigger_mode"] in ("event", "both"):
            self._register_notice_hooks()

    # ================= 配置读取 =================
    def _mode(self) -> str:
        mode = str(self.config.get("record_mode", "per_group"))
        return mode if mode in ("per_group", "global") else "per_group"

    def _crawl_cfg(self) -> dict[str, Any]:
        c = self.config.get("crawl", {}) or {}
        return {
            "group_whitelist": [str(g) for g in (c.get("group_whitelist") or [])],
            "trigger_word": str(c.get("trigger_word", "")),
            "parse_mode": str(c.get("parse_mode", "csv")),
            "regex": str(c.get("regex", "")),
            "trigger_mode": str(c.get("trigger_mode", "both")),
            "poll_interval_minutes": int(c.get("poll_interval_minutes", 30) or 30),
        }

    def _at_all_cfg(self) -> dict[str, Any]:
        c = self.config.get("at_all", {}) or {}
        return {
            "enable": bool(c.get("enable", False)),
            "group_whitelist": [str(g) for g in (c.get("group_whitelist") or [])],
            "user_whitelist": [str(u) for u in (c.get("user_whitelist") or [])],
        }

    def _template(self) -> str:
        return str(
            self.config.get(
                "reminder_template",
                "今天是 {at} 的生日，让我们祝 {nick} 生日快乐！！！🎂🥳",
            )
        )

    def _list_count(self) -> int:
        return int(self.config.get("list_count", 10) or 10)

    def _feb29_mode(self) -> str:
        mode = str(self.config.get("feb29_mode", "feb28"))
        return mode if mode in ("feb28", "mar1", "none") else "feb28"

    def _parse_check_time(self) -> tuple[int, int]:
        raw = str(self.config.get("check_time", "00:00"))
        try:
            hh, mm = raw.split(":")
            return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
        except Exception:  # noqa: BLE001
            logger.warning(f"[birthday] check_time 格式错误: {raw!r}，回退到 00:00")
            return 0, 0

    # ================= 指令 =================
    @filter.command_group("birthday")
    def birthday(self):  # noqa: D401 - 指令组占位
        """生日提醒相关指令。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @birthday.command("add")
    async def birthday_add(
        self,
        event: AstrMessageEvent,
        uin: str,
        nick: str,
        year: int,
        month: int,
        day: int,
    ):
        """添加生日：/birthday add <uin> <nick> <year> <month> <day>"""
        mode = self._mode()
        group_id = event.get_group_id() or ""
        record = BirthdayRecord(
            uin=str(uin),
            nick=str(nick),
            year=int(year),
            month=int(month),
            day=int(day),
            group_id="" if mode == "global" else group_id,
            platform_id=event.get_platform_id(),
            umo=event.unified_msg_origin,
            source="manual",
        )
        if not record.is_valid_date():
            yield event.plain_result(f"❌ 日期非法：{month}-{day}")
            return

        records = await self.store.get_records(mode)
        if self._find_existing(records, mode, record.group_id, record.uin) is not None:
            scope = "全局" if mode == "global" else "本群"
            yield event.plain_result(f"⚠️ {scope}已存在 QQ {record.uin} 的生日记录，未覆盖。")
            return

        records.append(record)
        await self.store.save_records(mode, records)
        yield event.plain_result(f"✅ 已添加：{record.nick}({record.uin}) 生日 {record.date_str()}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @birthday.command("del")
    async def birthday_del(self, event: AstrMessageEvent, target: str):
        """删除生日：/birthday del <uin 或 nick>（精准匹配，先 uin 后 nick）"""
        mode = self._mode()
        group_id = event.get_group_id() or ""
        records = await self.store.get_records(mode)
        in_scope = [r for r in records if mode == "global" or r.group_id == group_id]

        target = str(target)
        matched = [r for r in in_scope if r.uin == target]
        if not matched:
            matched = [r for r in in_scope if r.nick == target]
        if not matched:
            yield event.plain_result(f"❌ 未找到精准匹配「{target}」的记录。")
            return

        remaining = [r for r in records if r not in matched]
        await self.store.save_records(mode, remaining)
        names = "、".join(f"{r.nick}({r.uin})" for r in matched)
        yield event.plain_result(f"🗑️ 已删除 {len(matched)} 条记录：{names}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @birthday.command("list")
    async def birthday_list(self, event: AstrMessageEvent, scope: str = ""):
        """查看生日：/birthday list [all]"""
        mode = self._mode()
        group_id = event.get_group_id() or ""
        show_all = scope.strip().lower() == "all"
        records = await self.store.get_records(mode)

        conflict_bucket = "" if (mode == "global" or show_all) else group_id
        conflicts = await self.store.get_conflicts(mode, conflict_bucket)

        subset = await self._scope_records(records, mode, group_id, show_all, event)
        feb29_mode = self._feb29_mode()
        upcoming = upcoming_birthdays(subset, self._list_count(), feb29_mode=feb29_mode)

        header = "🎂 即将过生日（本群）" if not show_all else "🎂 即将过生日（全部）"
        lines: list[str] = []
        if conflicts:
            lines.append("⚠️ 上次抓取存在以下问题：")
            lines.extend(f"  - {c}" for c in conflicts[:20])
            lines.append("")
        lines.append(header)
        if not upcoming:
            lines.append("（暂无记录）")
        else:
            today = datetime.date.today()
            for i, r in enumerate(upcoming, start=1):
                delta = (r.next_birthday(today, feb29_mode) - today).days
                when = "今天！🎉" if delta == 0 else f"还有 {delta} 天"
                age = r.compute_age()
                age_s = f"，{age} 岁" if age is not None else ""
                lines.append(f"{i}. {r.nick}({r.uin}) {r.date_str()} - {when}{age_s}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @birthday.command("crawl")
    async def birthday_crawl(self, event: AstrMessageEvent):
        """从群公告抓取生日信息：/birthday crawl"""
        yield event.plain_result("⏳ 开始抓取群公告……")
        added, skipped, groups = await self._crawl_all_groups()
        yield event.plain_result(
            f"✅ 抓取完成：处理 {groups} 个群，新增 {added} 条，跳过/冲突 {skipped} 条。\n"
            f"如有冲突详情，可通过 /birthday list 查看（显示在列表上方）。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @birthday.command("trigger")
    async def birthday_trigger(self, event: AstrMessageEvent):
        """手动触发生日检测：/birthday trigger"""
        yield event.plain_result("⏳ 开始检测今日生日……")
        sent = await self._run_birthday_check()
        yield event.plain_result(f"✅ 检测完成，已发送 {sent} 条生日提醒。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @birthday.command("test")
    async def birthday_test(
        self,
        event: AstrMessageEvent,
        uin: str,
        nick: str,
        year: int,
        month: int,
        day: int,
    ):
        """测试提醒效果：/birthday test <uin> <nick> <year> <month> <day>"""
        group_id = event.get_group_id() or ""
        record = BirthdayRecord(
            uin=str(uin),
            nick=str(nick),
            year=int(year),
            month=int(month),
            day=int(day),
            group_id=group_id,
            platform_id=event.get_platform_id(),
            umo=event.unified_msg_origin,
        )
        at_all = False
        client = getattr(event, "bot", None)
        if client is not None and group_id:
            at_all = await self._resolve_at_all(client, group_id, record.uin)
        chain = build_astrbot_chain(self._template(), record, at_all)
        yield event.chain_result(chain)

    # ================= 核心逻辑 =================
    @staticmethod
    def _find_existing(
        records: list[BirthdayRecord], mode: str, group_id: str, uin: str
    ) -> BirthdayRecord | None:
        for r in records:
            if r.uin == uin and (mode == "global" or r.group_id == group_id):
                return r
        return None

    async def _scope_records(
        self,
        records: list[BirthdayRecord],
        mode: str,
        group_id: str,
        show_all: bool,
        event: AstrMessageEvent,
    ) -> list[BirthdayRecord]:
        """按 list 语义筛选记录。"""
        if show_all:
            return records
        if mode == "per_group":
            return [r for r in records if r.group_id == group_id]
        # global 模式：群内按群成员过滤，私聊则展示全部
        if not group_id:
            return records
        client = getattr(event, "bot", None)
        if client is None:
            return records
        members = await self._get_group_members(client, group_id)
        if members is None:
            return records
        return [r for r in records if r.uin in members]

    async def _crawl_all_groups(self) -> tuple[int, int, int]:
        """抓取所有白名单群公告。返回 (新增, 跳过, 处理群数)。"""
        crawl = self._crawl_cfg()
        whitelist = crawl["group_whitelist"]
        logger.debug(
            f"[birthday] 开始抓取：group_whitelist={whitelist} "
            f"trigger_word={crawl['trigger_word']!r} parse_mode={crawl['parse_mode']!r} "
            f"trigger_mode={crawl['trigger_mode']!r}"
        )
        if not whitelist:
            logger.debug("[birthday] group_whitelist 为空，跳过抓取")
            return 0, 0, 0
        clients = self._get_aiocqhttp_clients()
        logger.debug(f"[birthday] 可用 aiocqhttp client 数量={len(clients)}")
        if not clients:
            logger.warning("[birthday] 未找到 aiocqhttp 平台，无法抓取")
            return 0, 0, 0
        # 单 bot 假设：使用第一个可用 client
        platform_id, client = clients[0]
        logger.debug(f"[birthday] 使用 platform_id={platform_id!r} 进行抓取")

        mode = self._mode()
        records = await self.store.get_records(mode)
        total_added = total_skipped = 0
        global_conflicts: list[str] = []
        for gid in whitelist:
            added, skipped, conflicts = await self._crawl_group(
                client, platform_id, gid, records, mode, crawl
            )
            total_added += added
            total_skipped += skipped
            if mode == "per_group":
                await self.store.set_conflicts(mode, gid, conflicts)
            else:
                global_conflicts.extend(conflicts)
        await self.store.save_records(mode, records)
        if mode == "global":
            await self.store.set_conflicts(mode, "", global_conflicts)
        return total_added, total_skipped, len(whitelist)

    async def _crawl_group(
        self,
        client: Any,
        platform_id: str,
        group_id: str,
        records: list[BirthdayRecord],
        mode: str,
        crawl: dict[str, Any],
    ) -> tuple[int, int, list[str]]:
        """抓取单个群。原地更新 records。返回 (新增, 跳过, 冲突信息)。"""
        logger.debug(f"[birthday] === 抓取群 {group_id} 开始 ===")
        notices = await fetch_group_notices(client, group_id)
        processed = await self.store.get_processed(group_id)
        logger.debug(
            f"[birthday] 群 {group_id} 已处理过的公告 id 数量={len(processed)}: {sorted(processed)}"
        )
        new_ids: list[str] = []
        added = skipped = 0
        conflicts: list[str] = []
        trigger_word = crawl["trigger_word"]

        for notice in notices:
            nid = notice["notice_id"]
            if nid in processed:
                logger.debug(f"[birthday] 群 {group_id} 公告 {nid} 已处理过，跳过")
                continue
            new_ids.append(nid)
            text = notice["text"]
            if trigger_word and trigger_word not in text:
                logger.debug(
                    f"[birthday] 群 {group_id} 公告 {nid} 不含触发词 {trigger_word!r}，跳过解析"
                )
                continue
            logger.debug(f"[birthday] 群 {group_id} 公告 {nid} 命中触发词，开始解析")
            entries, errors = parse_announcement(text, crawl["parse_mode"], crawl["regex"])
            for err in errors:
                conflicts.append(f"[群{group_id}] {err}")
            for entry in entries:
                target_gid = "" if mode == "global" else group_id
                if self._find_existing(records, mode, target_gid, entry.uin) is not None:
                    skipped += 1
                    conflicts.append(f"[群{group_id}] QQ {entry.uin}({entry.nick}) 已存在，跳过")
                    continue
                records.append(
                    BirthdayRecord(
                        uin=entry.uin,
                        nick=entry.nick,
                        year=entry.year,
                        month=entry.month,
                        day=entry.day,
                        group_id=target_gid,
                        platform_id=platform_id,
                        umo=f"{platform_id}:GroupMessage:{group_id}",
                        source="crawl",
                    )
                )
                added += 1
        await self.store.add_processed(group_id, new_ids)
        logger.debug(
            f"[birthday] === 抓取群 {group_id} 结束：新增 {added}，跳过 {skipped}，"
            f"新处理公告 {len(new_ids)} 条，冲突/错误 {len(conflicts)} 条 ==="
        )
        return added, skipped, conflicts

    async def _run_birthday_check(self) -> int:
        """执行生日检测并发送提醒。返回发送条数。"""
        mode = self._mode()
        records = await self.store.get_records(mode)
        today = datetime.date.today()
        birthdays = find_today_birthdays(records, today, self._feb29_mode())
        logger.debug(
            f"[birthday] 生日检测：mode={mode} 今天={today} 记录总数={len(records)} "
            f"feb29_mode={self._feb29_mode()} 今日生日命中={len(birthdays)} "
            f"-> {[(b.uin, b.nick, b.month, b.day) for b in birthdays]}"
        )
        if not birthdays:
            return 0
        if mode == "per_group":
            return await self._send_per_group(birthdays)
        return await self._send_global(birthdays)

    async def _send_per_group(self, birthdays: list[BirthdayRecord]) -> int:
        sent = 0
        template = self._template()
        for r in birthdays:
            try:
                client = self._get_client_by_platform_id(r.platform_id)
                if r.group_id and client is not None:
                    at_all = await self._resolve_at_all(client, r.group_id, r.uin)
                    msg = build_onebot_message(template, r, at_all)
                    await client.api.call_action(
                        "send_group_msg", group_id=int(r.group_id), message=msg
                    )
                    sent += 1
                elif r.umo:
                    chain = build_astrbot_chain(template, r, False)
                    await self.context.send_message(r.umo, MessageChain(chain=chain))
                    sent += 1
            except Exception as e:  # noqa: BLE001
                logger.error(f"[birthday] 发送提醒失败 uin={r.uin}: {e}")
        return sent

    async def _send_global(self, birthdays: list[BirthdayRecord]) -> int:
        """global 模式：向白名单群中该用户所在的群发送提醒。"""
        crawl = self._crawl_cfg()
        whitelist = crawl["group_whitelist"]
        clients = self._get_aiocqhttp_clients()
        if not clients or not whitelist:
            logger.warning("[birthday] global 模式缺少可用 client 或抓取群白名单")
            return 0
        _, client = clients[0]
        template = self._template()
        sent = 0
        for gid in whitelist:
            members = await self._get_group_members(client, gid)
            if members is None:
                continue
            for r in birthdays:
                if r.uin not in members:
                    continue
                try:
                    at_all = await self._resolve_at_all(client, gid, r.uin)
                    msg = build_onebot_message(template, r, at_all)
                    await client.api.call_action("send_group_msg", group_id=int(gid), message=msg)
                    sent += 1
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[birthday] global 发送提醒失败 uin={r.uin}: {e}")
        return sent

    # ================= 定时任务回调 =================
    async def _job_birthday_check(self) -> None:
        try:
            sent = await self._run_birthday_check()
            logger.info(f"[birthday] 每日生日检测完成，发送 {sent} 条提醒")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[birthday] 每日生日检测异常: {e}")

    async def _job_poll_crawl(self) -> None:
        try:
            added, skipped, groups = await self._crawl_all_groups()
            if added or skipped:
                logger.info(
                    f"[birthday] 轮询抓取：新增 {added} 条，跳过 {skipped} 条（{groups} 群）"
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[birthday] 轮询抓取异常: {e}")

    # ================= 协议端事件监听 =================
    def _register_notice_hooks(self) -> None:
        if self._notice_hooked:
            return
        clients = self._get_aiocqhttp_clients()
        if not clients:
            return
        for platform_id, client in clients:
            try:
                client.on_notice(self._make_notice_handler(platform_id))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[birthday] 注册 notice 监听失败: {e}")
        self._notice_hooked = True
        logger.info("[birthday] 已注册协议端群公告/通知事件监听")

    def _make_notice_handler(self, platform_id: str):
        async def _handler(ev: Any) -> None:
            try:
                gid = str(ev.get("group_id", "") if hasattr(ev, "get") else "")
            except Exception:  # noqa: BLE001
                gid = ""
            if not gid:
                return
            whitelist = self._crawl_cfg()["group_whitelist"]
            if gid not in whitelist:
                return
            now = time.monotonic()
            last = self._notice_last_crawl.get(gid, 0.0)
            if now - last < _NOTICE_DEBOUNCE_SECONDS:
                return
            self._notice_last_crawl[gid] = now
            asyncio.create_task(self._notice_trigger_crawl(platform_id, gid))

        return _handler

    async def _notice_trigger_crawl(self, platform_id: str, group_id: str) -> None:
        try:
            client = self._get_client_by_platform_id(platform_id)
            if client is None:
                return
            crawl = self._crawl_cfg()
            mode = self._mode()
            records = await self.store.get_records(mode)
            added, skipped, conflicts = await self._crawl_group(
                client, platform_id, group_id, records, mode, crawl
            )
            await self.store.save_records(mode, records)
            bucket = "" if mode == "global" else group_id
            if conflicts:
                await self.store.set_conflicts(mode, bucket, conflicts)
            if added or skipped:
                logger.info(f"[birthday] 事件触发抓取群 {group_id}：新增 {added}，跳过 {skipped}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[birthday] 事件触发抓取异常: {e}")

    # ================= 平台/协议端辅助 =================
    def _get_aiocqhttp_clients(self) -> list[tuple[str, Any]]:
        """返回 [(platform_id, client), ...]。"""
        result: list[tuple[str, Any]] = []
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (  # noqa: E501
                AiocqhttpAdapter,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[birthday] 无法导入 AiocqhttpAdapter: {e}")
            return result
        try:
            insts = self.context.platform_manager.get_insts()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[birthday] 获取平台实例失败: {e}")
            return result
        for inst in insts:
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            try:
                client = inst.get_client()
            except Exception:  # noqa: BLE001
                continue
            if client is not None:
                result.append((self._platform_id_of(inst), client))
        return result

    @staticmethod
    def _platform_id_of(inst: Any) -> str:
        meta = getattr(inst, "metadata", None)
        if meta is None and hasattr(inst, "meta"):
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001
                meta = None
        for attr in ("id", "name"):
            val = getattr(meta, attr, None)
            if val:
                return str(val)
        return "aiocqhttp"

    def _get_client_by_platform_id(self, platform_id: str) -> Any:
        if platform_id:
            try:
                inst = self.context.get_platform_inst(platform_id)
                if inst is not None and hasattr(inst, "get_client"):
                    return inst.get_client()
            except Exception:  # noqa: BLE001
                pass
        clients = self._get_aiocqhttp_clients()
        return clients[0][1] if clients else None

    async def _get_self_id(self, client: Any) -> str:
        key = id(client)
        if key in self._self_id_cache:
            return self._self_id_cache[key]
        try:
            info = await client.api.call_action("get_login_info")
            self_id = str(info.get("user_id", ""))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[birthday] 获取登录信息失败: {e}")
            self_id = ""
        self._self_id_cache[key] = self_id
        return self_id

    async def _is_bot_admin(self, client: Any, group_id: str) -> bool:
        self_id = await self._get_self_id(client)
        if not self_id:
            return False
        try:
            info = await client.api.call_action(
                "get_group_member_info", group_id=int(group_id), user_id=int(self_id)
            )
            return str(info.get("role", "")) in ("admin", "owner")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[birthday] 获取 bot 群角色失败: {e}")
            return False

    async def _resolve_at_all(self, client: Any, group_id: str, uin: str) -> bool:
        cfg = self._at_all_cfg()
        if not at_all_allowed_by_config(
            cfg["enable"], cfg["group_whitelist"], cfg["user_whitelist"], group_id, uin
        ):
            return False
        return await self._is_bot_admin(client, group_id)

    async def _get_group_members(self, client: Any, group_id: str) -> set[str] | None:
        try:
            members = await client.api.call_action("get_group_member_list", group_id=int(group_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[birthday] 获取群 {group_id} 成员失败: {e}")
            return None
        if not isinstance(members, list):
            return None
        return {str(m.get("user_id", "")) for m in members if isinstance(m, dict)}
