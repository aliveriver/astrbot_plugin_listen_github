import asyncio
from typing import List, Optional

import aiohttp
import feedparser

from astrbot.api import logger
from astrbot.api.event import MessageChain

try:
    from .github_shared import (
        KV_INITIALIZED_PREFIX,
        KV_LAST_ENTRY_PREFIX,
        MAX_SCAN_ENTRIES,
        convert_time,
        extract_content,
        format_entries,
        normalize_watch_list,
    )
except ImportError:
    from github_shared import (
        KV_INITIALIZED_PREFIX,
        KV_LAST_ENTRY_PREFIX,
        MAX_SCAN_ENTRIES,
        convert_time,
        extract_content,
        format_entries,
        normalize_watch_list,
    )


class GitHubRssMixin:
    """负责 RSS 拉取、轮询与推送。"""

    async def initialize(self):
        """初始化插件并启动轮询任务。"""
        self.cfg_watch_users = normalize_watch_list(self.cfg_watch_users, "user", logger)
        self.cfg_watch_repos = normalize_watch_list(self.cfg_watch_repos, "repo", logger)
        self.cfg_watch_repos_commits = normalize_watch_list(self.cfg_watch_repos_commits, "repo", logger)

        logger.info(
            f"[GitHub Listen] 已初始化，轮询间隔：{self.poll_interval} 秒，"
            f"用户：{self.cfg_watch_users}，"
            f"Release 仓库：{self.cfg_watch_repos}，"
            f"Commit 仓库：{self.cfg_watch_repos_commits}，"
            f"绑定会话：{len(self.cfg_bound_sessions)}"
        )
        self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        await self._init_cursors()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _init_cursors(self):
        """为所有监听目标初始化游标，避免首次启动推送历史消息。"""
        for feed_url, display_name, kv_key, _ in self._build_targets():
            if kv_key in self._initialized_keys:
                continue
            init_flag = f"{KV_INITIALIZED_PREFIX}{kv_key}"
            if await self.get_kv_data(init_flag, ""):
                self._initialized_keys.add(kv_key)
                continue
            feed = await self._fetch_feed(feed_url)
            if feed and feed.entries:
                first_id = feed.entries[0].get("id", feed.entries[0].get("link", ""))
                await self.put_kv_data(f"{KV_LAST_ENTRY_PREFIX}{kv_key}", first_id)
                await self.put_kv_data(init_flag, "1")
                self._initialized_keys.add(kv_key)
                logger.info(f"[GitHub Listen] 已初始化游标: {kv_key}")
            else:
                logger.warning(f"[GitHub Listen] 初始化游标失败，稍后重试: {display_name}")

    async def _poll_loop(self):
        """定时轮询 GitHub 动态。"""
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
        """执行一次轮询并按订阅关系分发消息。"""
        targets = self._build_targets()
        if not targets or not self.cfg_bound_sessions:
            return

        ready_targets = [t for t in targets if t[2] in self._initialized_keys]
        if not ready_targets:
            return

        results = await asyncio.gather(
            *[self._fetch_new_entries(t[0], t[2]) for t in ready_targets],
            return_exceptions=True,
        )

        send_tasks = []
        for target, result in zip(ready_targets, results):
            _, display_name, _, sub_id = target
            if isinstance(result, Exception):
                logger.error(f"[GitHub Listen] 获取 {display_name} 失败: {result}")
                continue
            if not result:
                continue
            chain = MessageChain().message(format_entries(display_name, result))
            for umo in self.cfg_bound_sessions:
                if self._session_should_receive(umo, sub_id):
                    send_tasks.append(self._safe_send(umo, chain))

        if send_tasks:
            await asyncio.gather(*send_tasks)

    async def _safe_send(self, umo: str, chain: MessageChain):
        """安全发送消息，避免单个会话异常影响轮询。"""
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[GitHub Listen] 推送失败({umo}): {e}")

    async def _fetch_feed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        """拉取并解析 RSS/Atom。"""
        if not self._http_session or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
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
        """获取新条目并更新游标。"""
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
                "published": self._convert_time(entry.get("published") or entry.get("updated", "")),
                "content": self._extract_content(entry),
            })

        if self.max_entries > 0:
            new_entries = new_entries[: self.max_entries]
        if new_entries:
            await self.put_kv_data(full_kv_key, new_entries[0]["id"])
        return new_entries

    def _convert_time(self, time_str: str) -> str:
        """转换为当前配置时区。"""
        return convert_time(time_str, self.cfg_timezone)

    @staticmethod
    def _extract_content(entry) -> str:
        """提取 RSS 条目正文。"""
        return extract_content(entry)

    async def terminate(self):
        """停止轮询并关闭网络会话。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        logger.info("[GitHub Listen] 插件已卸载")
