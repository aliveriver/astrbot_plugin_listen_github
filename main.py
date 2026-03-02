import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import aiohttp
import feedparser

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

GITHUB_USER_FEED = "https://github.com/{username}.atom"
GITHUB_REPO_FEED = "https://github.com/{repo}/releases.atom"
GITHUB_REPO_COMMITS_FEED = "https://github.com/{repo}/commits.atom"
KV_WATCH_LIST = "watch_list"  # dict: {umo: [username, ...]}  (指令动态添加)
KV_LAST_ENTRY_PREFIX = "last_entry_"  # last_entry_{key} -> entry_id


@register(
    "astrbot_plugin_listen_github",
    "AliveRiver",
    "通过 RSS 定时获取 GitHub 用户/仓库动态并推送到聊天会话",
    "1.0.0",
    "https://github.com/aliveriver/astrbot_plugin_listen_github",
)
class GitHubListenPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.poll_interval: int = max(
            config.get("poll_interval", 1800), 60
        )  # 最低 60 秒
        self.max_entries: int = config.get("max_entries", 5)
        # JSON 配置中的监听列表
        self.cfg_watch_users: List[str] = config.get("watch_users", [])
        self.cfg_watch_repos: List[str] = config.get("watch_repos", [])
        self.cfg_watch_repos_commits: List[str] = config.get("watch_repos_commits", [])
        self.cfg_bound_sessions: List[str] = config.get("bound_sessions", [])
        self.cfg_timezone: str = config.get("timezone", "Asia/Shanghai")
        self._poll_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """插件初始化：启动定时轮询任务"""
        logger.info(
            f"[GitHub Listen] 插件初始化，轮询间隔: {self.poll_interval} 秒，"
            f"每次最多推送: {self.max_entries} 条，"
            f"配置监听用户: {self.cfg_watch_users}，"
            f"配置监听仓库(Release): {self.cfg_watch_repos}，"
            f"配置监听仓库(Commit): {self.cfg_watch_repos_commits}，"
            f"配置绑定会话: {len(self.cfg_bound_sessions)} 个"
        )
        self._poll_task = asyncio.create_task(self._poll_loop())

    # ==================== 定时轮询 ====================

    async def _poll_loop(self):
        """定时轮询所有监听项的 GitHub 动态"""
        await asyncio.sleep(10)  # 启动后等待 10 秒，确保平台连接就绪
        while True:
            try:
                await self._do_poll()
            except asyncio.CancelledError:
                logger.info("[GitHub Listen] 轮询任务已取消")
                return
            except Exception as e:
                logger.error(f"[GitHub Listen] 轮询出错: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _do_poll(self):
        """执行一次完整的轮询：合并 JSON 配置 + 指令动态添加的监听列表"""
        # 1) 收集 JSON 配置中要推送到所有 bound_sessions 的目标
        cfg_targets = []  # [(feed_url, display_name, kv_key), ...]
        for user in self.cfg_watch_users:
            cfg_targets.append(
                (
                    GITHUB_USER_FEED.format(username=user),
                    f"user {user}",
                    f"user_{user}",
                )
            )
        for repo in self.cfg_watch_repos:
            cfg_targets.append(
                (
                    GITHUB_REPO_FEED.format(repo=repo),
                    f"📦 {repo} (Release)",
                    f"repo_rel_{repo.replace('/', '_')}",
                )
            )
        for repo in self.cfg_watch_repos_commits:
            cfg_targets.append(
                (
                    GITHUB_REPO_COMMITS_FEED.format(repo=repo),
                    f"📝 {repo} (Commit)",
                    f"repo_cmt_{repo.replace('/', '_')}",
                )
            )

        # 推送配置中的目标到所有绑定会话
        for feed_url, display_name, kv_key in cfg_targets:
            new_entries = await self._fetch_new_entries(feed_url, kv_key)
            if new_entries:
                msg = self._format_entries(display_name, new_entries)
                chain = MessageChain().message(msg)
                for umo in self.cfg_bound_sessions:
                    try:
                        await self.context.send_message(umo, chain)
                    except Exception as e:
                        logger.error(f"[GitHub Listen] 推送到会话失败 ({umo}): {e}")

        # 2) 处理通过指令动态添加的监听（按会话分组）
        cmd_watch_list = await self._get_cmd_watch_list()
        for umo, usernames in cmd_watch_list.items():
            for username in usernames:
                feed_url = GITHUB_USER_FEED.format(username=username)
                kv_key = f"user_{username}"
                try:
                    new_entries = await self._fetch_new_entries(feed_url, kv_key)
                    if new_entries:
                        msg = self._format_entries(f"👤 {username}", new_entries)
                        chain = MessageChain().message(msg)
                        await self.context.send_message(umo, chain)
                except Exception as e:
                    logger.error(f"[GitHub Listen] 获取 {username} 动态失败: {e}")

    # ==================== RSS 获取与解析 ====================

    async def _fetch_feed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        """异步获取并解析 Atom Feed"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[GitHub Listen] 获取 Feed 失败: {url} -> HTTP {resp.status}"
                        )
                        return None
                    text = await resp.text()
                    return feedparser.parse(text)
        except Exception as e:
            logger.error(f"[GitHub Listen] 请求 Feed 异常: {url} -> {e}")
            return None

    async def _fetch_new_entries(self, feed_url: str, kv_key: str) -> List[dict]:
        """获取某 Feed 相比上次推送的新动态条目"""
        feed = await self._fetch_feed(feed_url)
        if not feed or not feed.entries:
            return []

        full_kv_key = f"{KV_LAST_ENTRY_PREFIX}{kv_key}"
        last_entry_id = await self.get_kv_data(full_kv_key, "")

        new_entries = []
        scan_entries = (
            feed.entries
            if self.max_entries == 0
            else feed.entries[: self.max_entries * 2]
        )
        for entry in scan_entries:
            entry_id = entry.get("id", entry.get("link", ""))
            if entry_id == last_entry_id:
                break
            new_entries.append(
                {
                    "id": entry_id,
                    "title": entry.get("title", "无标题").strip(),
                    "link": entry.get("link", "").strip(),
                    "published": self._convert_time(entry.get("published", "")),
                    "content": self._extract_content(entry),
                }
            )

        if self.max_entries > 0:
            new_entries = new_entries[: self.max_entries]

        if new_entries:
            await self.put_kv_data(full_kv_key, new_entries[0]["id"])

        return new_entries

    def _convert_time(self, time_str: str) -> str:
        """将 Feed 中的时间字符串转换为用户配置的时区"""
        if not time_str:
            return ""
        try:
            # GitHub Atom Feed 常见格式: "2026-03-02T01:26:27-08:00" 或 "2026-03-02 01:26:27 -0800"
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%SZ",
            ):
                try:
                    dt = datetime.strptime(time_str.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                # feedparser 解析的 time_struct
                if hasattr(feedparser, "_parse_date") and hasattr(
                    feedparser._parse_date, "__call__"
                ):
                    pass
                return time_str  # 无法解析则原样返回

            # 如果没有时区信息，假定 UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            # 转换到用户设定的时区
            local_tz = ZoneInfo(self.cfg_timezone)
            dt_local = dt.astimezone(local_tz)
            return dt_local.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return time_str  # 出错时原样返回

    @staticmethod
    def _extract_content(entry) -> str:
        """提取 Feed 条目的内容摘要，清理 HTML 标签和多余空白"""
        content = ""
        if hasattr(entry, "summary"):
            content = entry.summary
        elif hasattr(entry, "content") and entry.content:
            content = entry.content[0].get("value", "")
        # 去除 HTML 标签
        content = re.sub(r"<[^>]+>", " ", content)
        # 合并连续空白字符（空格、换行、制表符）为单个空格
        content = re.sub(r"\s+", " ", content).strip()
        if len(content) > 200:
            content = content[:200] + "..."
        return content

    # ==================== 消息格式化 ====================

    @staticmethod
    def _format_entries(display_name: str, entries: List[dict]) -> str:
        """将多条新动态格式化为一条推送消息"""
        lines = [f"📢 {display_name} 有 {len(entries)} 条新动态：\n"]
        for i, entry in enumerate(entries, 1):
            lines.append(f"  {i}. {entry['title']}")
            if entry["published"]:
                lines.append(f"     🕐 {entry['published']}")
            if entry["content"]:
                lines.append(f"     📝 {entry['content']}")
            if entry["link"]:
                lines.append(f"     🔗 {entry['link']}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_single_check(display_name: str, entries: List[dict]) -> str:
        """格式化立即查看的结果"""
        if not entries:
            return f"🔍 {display_name} 暂无最近的公开动态。"
        lines = [f"🔍 {display_name} 最近的动态：\n"]
        for i, entry in enumerate(entries, 1):
            lines.append(f"  {i}. {entry['title']}")
            if entry["published"]:
                lines.append(f"     🕐 {entry['published']}")
            if entry["content"]:
                lines.append(f"     📝 {entry['content']}")
            if entry["link"]:
                lines.append(f"     🔗 {entry['link']}")
            lines.append("")
        return "\n".join(lines)

    # ==================== KV 存储辅助 ====================

    async def _get_cmd_watch_list(self) -> Dict[str, List[str]]:
        """获取通过指令动态添加的监听列表"""
        data = await self.get_kv_data(KV_WATCH_LIST, "{}")
        if isinstance(data, str):
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return {}
        if isinstance(data, dict):
            return data
        return {}

    async def _save_cmd_watch_list(self, watch_list: Dict[str, List[str]]):
        """保存通过指令动态添加的监听列表"""
        await self.put_kv_data(
            KV_WATCH_LIST, json.dumps(watch_list, ensure_ascii=False)
        )

    # ==================== 指令处理 ====================

    @filter.command("gh_watch")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_watch(self, event: AstrMessageEvent):
        """添加 GitHub 用户到当前会话的监听列表。用法：/gh_watch <用户名>"""
        username = event.message_str.strip()
        if not username:
            yield event.plain_result(
                "❌ 请提供 GitHub 用户名，例如：/gh_watch torvalds"
            )
            return

        feed = await self._fetch_feed(GITHUB_USER_FEED.format(username=username))
        if feed is None or (hasattr(feed, "bozo") and feed.bozo and not feed.entries):
            yield event.plain_result(
                f"❌ 无法获取用户 {username} 的 GitHub 动态，请检查用户名是否正确。"
            )
            return

        umo = event.unified_msg_origin
        watch_list = await self._get_cmd_watch_list()

        if umo not in watch_list:
            watch_list[umo] = []

        if username.lower() in [u.lower() for u in watch_list[umo]]:
            yield event.plain_result(f"⚠️ 用户 {username} 已在当前会话的监听列表中。")
            return

        watch_list[umo].append(username)
        await self._save_cmd_watch_list(watch_list)

        # 记录当前最新条目 ID，避免下次轮询推送旧动态
        if feed and feed.entries:
            first_id = feed.entries[0].get("id", feed.entries[0].get("link", ""))
            await self.put_kv_data(f"{KV_LAST_ENTRY_PREFIX}user_{username}", first_id)

        yield event.plain_result(
            f"✅ 已添加 GitHub 用户 {username} 到监听列表。\n"
            f"将每 {self.poll_interval} 秒检查一次新动态。"
        )

    @filter.command("gh_unwatch")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_unwatch(self, event: AstrMessageEvent):
        """从当前会话移除 GitHub 用户的监听。用法：/gh_unwatch <用户名>"""
        username = event.message_str.strip()
        if not username:
            yield event.plain_result(
                "❌ 请提供 GitHub 用户名，例如：/gh_unwatch torvalds"
            )
            return

        umo = event.unified_msg_origin
        watch_list = await self._get_cmd_watch_list()

        if umo not in watch_list or not watch_list[umo]:
            yield event.plain_result("⚠️ 当前会话没有通过指令监听任何 GitHub 用户。")
            return

        matched = [u for u in watch_list[umo] if u.lower() == username.lower()]
        if not matched:
            yield event.plain_result(
                f"⚠️ 用户 {username} 不在当前会话的指令监听列表中。"
            )
            return

        for m in matched:
            watch_list[umo].remove(m)
        if not watch_list[umo]:
            del watch_list[umo]

        await self._save_cmd_watch_list(watch_list)
        yield event.plain_result(f"✅ 已从监听列表移除 GitHub 用户 {username}。")

    @filter.command("gh_list")
    async def gh_list(self, event: AstrMessageEvent):
        """列出当前会话和全局配置中监听的所有 GitHub 用户/仓库"""
        umo = event.unified_msg_origin
        watch_list = await self._get_cmd_watch_list()
        cmd_users = watch_list.get(umo, [])

        lines = ["📋 GitHub 动态监听列表\n"]

        # JSON 配置中的全局监听
        if self.cfg_watch_users or self.cfg_watch_repos:
            lines.append("── 全局配置（JSON）──")
            for u in self.cfg_watch_users:
                lines.append(f"  👤 {u}")
            for r in self.cfg_watch_repos:
                lines.append(f"  📦 {r}")
            lines.append(f"  → 推送到 {len(self.cfg_bound_sessions)} 个绑定会话")
            lines.append("")

        # 指令动态添加的
        if cmd_users:
            lines.append("── 当前会话（指令添加）──")
            for i, u in enumerate(cmd_users, 1):
                lines.append(f"  {i}. 👤 {u}")
            lines.append("")

        if not self.cfg_watch_users and not self.cfg_watch_repos and not cmd_users:
            lines.append("暂无任何监听项。")
            lines.append("使用 /gh_watch <用户名> 添加，或在 WebUI 配置中设置。")

        lines.append(f"⏱️ 轮询间隔：{self.poll_interval} 秒")
        yield event.plain_result("\n".join(lines))

    @filter.command("gh_check")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_check(self, event: AstrMessageEvent):
        """立即检查某 GitHub 用户或仓库的最新动态。用法：/gh_check <用户名或owner/repo>"""
        target = event.message_str.strip()
        if not target:
            yield event.plain_result(
                "❌ 请提供目标，例如：\n"
                "  /gh_check torvalds（用户）\n"
                "  /gh_check microsoft/vscode（仓库）"
            )
            return

        # 判断是用户还是仓库
        if "/" in target:
            feed_url = GITHUB_REPO_FEED.format(repo=target)
            display_name = f"📦 {target}"
        else:
            feed_url = GITHUB_USER_FEED.format(username=target)
            display_name = f"👤 {target}"

        yield event.plain_result(f"🔄 正在获取 {display_name} 的最新动态...")

        feed = await self._fetch_feed(feed_url)
        if feed is None or not feed.entries:
            yield event.plain_result(
                f"❌ 无法获取 {display_name} 的动态，请检查名称是否正确。"
            )
            return

        entries = []
        check_entries = (
            feed.entries if self.max_entries == 0 else feed.entries[: self.max_entries]
        )
        for entry in check_entries:
            entries.append(
                {
                    "id": entry.get("id", ""),
                    "title": entry.get("title", "无标题").strip(),
                    "link": entry.get("link", "").strip(),
                    "published": self._convert_time(entry.get("published", "")),
                    "content": self._extract_content(entry),
                }
            )

        msg = self._format_single_check(display_name, entries)
        yield event.plain_result(msg)

    @filter.command("gh_bindhere")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_bindhere(self, event: AstrMessageEvent):
        """将当前会话绑定为推送目标（自动获取 unified_msg_origin 并添加到配置）"""
        umo = event.unified_msg_origin

        if umo in self.cfg_bound_sessions:
            yield event.plain_result("⚠️ 当前会话已在绑定列表中。")
            return

        self.cfg_bound_sessions.append(umo)
        # 更新配置并持久化
        self.config["bound_sessions"] = self.cfg_bound_sessions
        self.config.save_config()

        yield event.plain_result(
            f"✅ 已绑定当前会话为推送目标。\n"
            f"会话标识：{umo}\n"
            f"当前共 {len(self.cfg_bound_sessions)} 个绑定会话。"
        )

    @filter.command("gh_unbindhere")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_unbindhere(self, event: AstrMessageEvent):
        """将当前会话从推送目标中移除"""
        umo = event.unified_msg_origin

        if umo not in self.cfg_bound_sessions:
            yield event.plain_result("⚠️ 当前会话不在绑定列表中。")
            return

        self.cfg_bound_sessions.remove(umo)
        self.config["bound_sessions"] = self.cfg_bound_sessions
        self.config.save_config()

        yield event.plain_result(
            f"✅ 已解绑当前会话。\n剩余 {len(self.cfg_bound_sessions)} 个绑定会话。"
        )

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载时取消定时任务"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("[GitHub Listen] 插件已卸载，定时任务已停止。")
