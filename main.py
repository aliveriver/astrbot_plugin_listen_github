import asyncio
import re
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import aiohttp
import feedparser

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

GITHUB_USER_FEED = "https://github.com/{username}.atom"
GITHUB_REPO_FEED = "https://github.com/{repo}/releases.atom"
GITHUB_REPO_COMMITS_FEED = "https://github.com/{repo}/commits.atom"
KV_LAST_ENTRY_PREFIX = "last_entry_"
KV_INITIALIZED_PREFIX = "initialized_"
MAX_SCAN_ENTRIES = 50  # 扫描窗口硬上限

# 合法的 GitHub 用户名/仓库名校验
RE_USERNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?$")
RE_REPO = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


@register(
    "astrbot_plugin_listen_github",
    "aliveriver",
    "通过 RSS 定时获取 GitHub 用户/仓库动态并推送到聊天会话",
    "1.0.0",
    "https://github.com/aliveriver/astrbot_plugin_listen_github",
)
class GitHubListenPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.poll_interval: int = max(config.get("poll_interval", 1800), 60)
        self.max_entries: int = max(config.get("max_entries", 5), 0)
        self.cfg_watch_users: List[str] = config.get("watch_users", [])
        self.cfg_watch_repos: List[str] = config.get("watch_repos", [])
        self.cfg_watch_repos_commits: List[str] = config.get("watch_repos_commits", [])
        self.cfg_bound_sessions: List[str] = config.get("bound_sessions", [])
        self.cfg_timezone: str = config.get("timezone", "Asia/Shanghai")
        self._poll_task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._initialized_keys: Set[str] = set()  # 已成功初始化游标的 kv_key
        self._config_lock = asyncio.Lock()  # 保护 bound_sessions 并发写

    async def initialize(self):
        """插件初始化"""
        logger.info(
            f"[GitHub Listen] 插件初始化，轮询间隔: {self.poll_interval} 秒，"
            f"监听用户: {self.cfg_watch_users}，"
            f"监听仓库(Release): {self.cfg_watch_repos}，"
            f"监听仓库(Commit): {self.cfg_watch_repos_commits}，"
            f"绑定会话: {len(self.cfg_bound_sessions)} 个"
        )
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        await self._init_cursors()
        self._poll_task = asyncio.create_task(self._poll_loop())

    # ==================== 目标构建 ====================

    def _build_targets(self) -> List[Tuple[str, str, str]]:
        """构建所有监听目标 [(feed_url, display_name, kv_key), ...]"""
        targets = []
        for user in self.cfg_watch_users:
            targets.append((
                GITHUB_USER_FEED.format(username=user),
                f"user {user}",
                f"user_{user}",
            ))
        for repo in self.cfg_watch_repos:
            targets.append((
                GITHUB_REPO_FEED.format(repo=repo),
                f"repo {repo} (Release)",
                f"repo_rel_{repo.replace('/', '_')}",
            ))
        for repo in self.cfg_watch_repos_commits:
            targets.append((
                GITHUB_REPO_COMMITS_FEED.format(repo=repo),
                f"repo {repo} (Commit)",
                f"repo_cmt_{repo.replace('/', '_')}",
            ))
        return targets

    async def _init_cursors(self):
        """为所有监听目标初始化游标，防止首次启动推送历史消息。
        仅在成功获取并写入游标后才标记已初始化。
        """
        for feed_url, display_name, kv_key in self._build_targets():
            if kv_key in self._initialized_keys:
                continue
            # 检查持久化标记
            init_flag = f"{KV_INITIALIZED_PREFIX}{kv_key}"
            if await self.get_kv_data(init_flag, ""):
                self._initialized_keys.add(kv_key)
                continue
            # 首次：拉取并写入游标
            feed = await self._fetch_feed(feed_url)
            if feed and feed.entries:
                first_id = feed.entries[0].get("id", feed.entries[0].get("link", ""))
                await self.put_kv_data(f"{KV_LAST_ENTRY_PREFIX}{kv_key}", first_id)
                await self.put_kv_data(init_flag, "1")
                self._initialized_keys.add(kv_key)
                logger.info(f"[GitHub Listen] 已初始化游标: {kv_key}")
            else:
                logger.warning(f"[GitHub Listen] 初始化游标失败，将在下次重试: {display_name}")

    # ==================== 定时轮询 ====================

    async def _poll_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._init_cursors()
                await self._do_poll()
            except asyncio.CancelledError:
                logger.info("[GitHub Listen] 轮询任务已取消")
                return
            except Exception as e:
                logger.error(f"[GitHub Listen] 轮询出错: {e}")
            await asyncio.sleep(self.poll_interval)

    async def _do_poll(self):
        """执行一次轮询：仅拉取已初始化的目标，推送到绑定会话"""
        targets = self._build_targets()
        if not targets or not self.cfg_bound_sessions:
            return

        # 只轮询已初始化的目标，未初始化的跳过（等下一轮 _init_cursors 重试）
        ready_targets = [t for t in targets if t[2] in self._initialized_keys]
        if not ready_targets:
            return

        # 并发拉取
        results = await asyncio.gather(
            *[self._fetch_new_entries(t[0], t[2]) for t in ready_targets],
            return_exceptions=True,
        )

        # 并发推送
        send_tasks = []
        for target, result in zip(ready_targets, results):
            _, display_name, _ = target
            if isinstance(result, Exception):
                logger.error(f"[GitHub Listen] 获取 {display_name} 失败: {result}")
                continue
            if not result:
                continue
            msg = self._format_entries(display_name, result)
            chain = MessageChain().message(msg)
            for umo in self.cfg_bound_sessions:
                send_tasks.append(self._safe_send(umo, chain))

        if send_tasks:
            await asyncio.gather(*send_tasks)

    async def _safe_send(self, umo: str, chain: MessageChain):
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[GitHub Listen] 推送失败 ({umo}): {e}")

    # ==================== RSS 获取与解析 ====================

    async def _fetch_feed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        if not self._http_session or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        try:
            async with self._http_session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[GitHub Listen] Feed 请求失败: {url} -> HTTP {resp.status}")
                    return None
                text = await resp.text()
                return feedparser.parse(text)
        except Exception as e:
            logger.error(f"[GitHub Listen] Feed 请求异常: {url} -> {e}")
            return None

    async def _fetch_new_entries(self, feed_url: str, kv_key: str) -> List[dict]:
        feed = await self._fetch_feed(feed_url)
        if not feed or not feed.entries:
            return []

        full_kv_key = f"{KV_LAST_ENTRY_PREFIX}{kv_key}"
        last_entry_id = await self.get_kv_data(full_kv_key, "")

        new_entries = []
        scan_limit = MAX_SCAN_ENTRIES if self.max_entries == 0 else max(self.max_entries * 2, MAX_SCAN_ENTRIES)
        for entry in feed.entries[:scan_limit]:
            entry_id = entry.get("id", entry.get("link", ""))
            if entry_id == last_entry_id:
                break
            new_entries.append({
                "id": entry_id,
                "title": entry.get("title", "无标题").strip(),
                "link": entry.get("link", "").strip(),
                "published": self._convert_time(
                    entry.get("published") or entry.get("updated", "")
                ),
                "content": self._extract_content(entry),
            })

        if self.max_entries > 0:
            new_entries = new_entries[: self.max_entries]
        if new_entries:
            await self.put_kv_data(full_kv_key, new_entries[0]["id"])
        return new_entries

    def _convert_time(self, time_str: str) -> str:
        if not time_str:
            return ""
        try:
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    dt = datetime.strptime(time_str.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return time_str
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(self.cfg_timezone)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return time_str

    @staticmethod
    def _extract_content(entry) -> str:
        content = ""
        if hasattr(entry, "summary"):
            content = entry.summary
        elif hasattr(entry, "content") and entry.content:
            content = entry.content[0].get("value", "")
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
        if len(content) > 200:
            content = content[:200] + "..."
        return content

    # ==================== 消息格式化 ====================

    @staticmethod
    def _format_entries(display_name: str, entries: List[dict]) -> str:
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

    # ==================== 指令 ====================

    @filter.command("gh_list")
    async def gh_list(self, event: AstrMessageEvent):
        """列出所有监听的 GitHub 用户/仓库"""
        lines = ["📋 GitHub 动态监听列表\n"]
        if self.cfg_watch_users:
            for u in self.cfg_watch_users:
                lines.append(f"  👤 {u}")
        if self.cfg_watch_repos:
            for r in self.cfg_watch_repos:
                lines.append(f"  📦 {r} (Release)")
        if self.cfg_watch_repos_commits:
            for r in self.cfg_watch_repos_commits:
                lines.append(f"  📝 {r} (Commit)")
        if not self.cfg_watch_users and not self.cfg_watch_repos and not self.cfg_watch_repos_commits:
            lines.append("暂无任何监听项，请在 WebUI 配置中设置。")
        else:
            lines.append(f"\n→ 推送到 {len(self.cfg_bound_sessions)} 个绑定会话")
        lines.append(f"⏱️ 轮询间隔：{self.poll_interval} 秒")
        yield event.plain_result("\n".join(lines))

    @filter.command("gh_check")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_check(self, event: AstrMessageEvent):
        """立即检查某 GitHub 用户或仓库的最新动态。
        用法：/gh_check <用户名>          查看用户动态
              /gh_check <owner/repo>      查看仓库最新 Release
              /gh_check <owner/repo> commit  查看仓库最新 Commit
        """
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "❌ 请提供目标，例如：\n"
                "  /gh_check torvalds（用户动态）\n"
                "  /gh_check microsoft/vscode（最新 Release）\n"
                "  /gh_check microsoft/vscode commit（最新 Commit）"
            )
            return

        target = parts[1]
        check_type = parts[2].lower() if len(parts) > 2 else None

        # 输入校验
        if "/" in target:
            if not RE_REPO.match(target):
                yield event.plain_result("❌ 仓库格式不正确，应为 owner/repo，如 microsoft/vscode")
                return
        else:
            if not RE_USERNAME.match(target):
                yield event.plain_result("❌ 用户名格式不正确，仅支持字母、数字、点、连字符和下划线")
                return

        if "/" in target:
            if check_type == "commit":
                feed_url = GITHUB_REPO_COMMITS_FEED.format(repo=target)
                display_name = f"📝 {target} (Commit)"
            else:
                feed_url = GITHUB_REPO_FEED.format(repo=target)
                display_name = f"📦 {target} (Release)"
        else:
            feed_url = GITHUB_USER_FEED.format(username=target)
            display_name = f"👤 {target}"

        yield event.plain_result(f"🔄 正在获取 {display_name} 的最新动态...")

        feed = await self._fetch_feed(feed_url)
        if feed is None or not feed.entries:
            yield event.plain_result(f"❌ 无法获取 {display_name} 的动态，请检查名称是否正确。")
            return

        entries = []
        check = feed.entries if self.max_entries == 0 else feed.entries[: self.max_entries]
        for entry in check:
            entries.append({
                "title": entry.get("title", "无标题").strip(),
                "link": entry.get("link", "").strip(),
                "published": self._convert_time(
                    entry.get("published") or entry.get("updated", "")
                ),
                "content": self._extract_content(entry),
            })

        yield event.plain_result(self._format_single_check(display_name, entries))

    @filter.command("gh_bindhere")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_bindhere(self, event: AstrMessageEvent):
        """将当前会话绑定为推送目标"""
        umo = event.unified_msg_origin
        async with self._config_lock:
            if umo in self.cfg_bound_sessions:
                yield event.plain_result("⚠️ 当前会话已在绑定列表中。")
                return
            self.cfg_bound_sessions.append(umo)
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
        """解绑当前会话"""
        umo = event.unified_msg_origin
        async with self._config_lock:
            if umo not in self.cfg_bound_sessions:
                yield event.plain_result("⚠️ 当前会话不在绑定列表中。")
                return
            self.cfg_bound_sessions.remove(umo)
            self.config["bound_sessions"] = self.cfg_bound_sessions
            self.config.save_config()
        yield event.plain_result(
            f"✅ 已解绑当前会话。\n"
            f"剩余 {len(self.cfg_bound_sessions)} 个绑定会话。"
        )

    # ==================== 生命周期 ====================

    async def terminate(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        logger.info("[GitHub Listen] 插件已卸载。")
