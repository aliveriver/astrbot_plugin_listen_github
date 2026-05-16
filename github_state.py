import asyncio
import json
from typing import List, Optional, Set, Tuple

import aiohttp

try:
    from .github_shared import (
        GITHUB_REPO_COMMITS_FEED,
        GITHUB_REPO_FEED,
        GITHUB_USER_FEED,
        parse_sub_id,
    )
except ImportError:
    from github_shared import (
        GITHUB_REPO_COMMITS_FEED,
        GITHUB_REPO_FEED,
        GITHUB_USER_FEED,
        parse_sub_id,
    )


class GitHubStateMixin:
    """负责配置状态、订阅映射与目标构建。"""

    def __init__(self, context, config):
        super().__init__(context)
        self.config = config
        self.poll_interval: int = max(config.get("poll_interval", 1800), 60)
        self.max_entries: int = max(config.get("max_entries", 5), 0)
        self.cfg_watch_users: List[str] = config.get("watch_users", [])
        self.cfg_watch_repos: List[str] = config.get("watch_repos", [])
        self.cfg_watch_repos_commits: List[str] = config.get("watch_repos_commits", [])
        self.cfg_bound_sessions: List[str] = config.get("bound_sessions", [])
        self.cfg_session_subs: dict = self._load_session_subscriptions(config)
        self.cfg_timezone: str = config.get("timezone", "Asia/Shanghai")
        self._poll_task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._initialized_keys: Set[str] = set()
        self._config_lock = asyncio.Lock()

    def _build_targets(self) -> List[Tuple[str, str, str, str]]:
        """构建所有监听目标。"""
        targets = []
        for user in self.cfg_watch_users:
            targets.append((
                GITHUB_USER_FEED.format(username=user),
                f"user {user}",
                f"user_{user}",
                f"user:{user.lower()}",
            ))
        for repo in self.cfg_watch_repos:
            targets.append((
                GITHUB_REPO_FEED.format(repo=repo),
                f"repo {repo} (Release)",
                f"repo_rel_{repo.replace('/', '_')}",
                f"repo:{repo.lower()}",
            ))
        for repo in self.cfg_watch_repos_commits:
            targets.append((
                GITHUB_REPO_COMMITS_FEED.format(repo=repo),
                f"repo {repo} (Commit)",
                f"repo_cmt_{repo.replace('/', '_')}",
                f"commits:{repo.lower()}",
            ))
        return targets

    def _get_all_sub_ids(self) -> List[str]:
        """获取当前所有可订阅的目标标识。"""
        ids = []
        for user in self.cfg_watch_users:
            ids.append(f"user:{user.lower()}")
        for repo in self.cfg_watch_repos:
            ids.append(f"repo:{repo.lower()}")
        for repo in self.cfg_watch_repos_commits:
            ids.append(f"commits:{repo.lower()}")
        return ids

    def _session_should_receive(self, umo: str, sub_id: str) -> bool:
        """判断某会话是否应接收某目标的推送。"""
        subs = self.cfg_session_subs.get(umo)
        if not subs:
            return True
        return sub_id in subs

    @staticmethod
    def _parse_sub_id(sub_id: str):
        """解析订阅标识。"""
        return parse_sub_id(sub_id)

    @staticmethod
    def _load_session_subscriptions(config) -> dict:
        """读取会话订阅配置，兼容旧 dict 字段和新的 JSON 文本字段。"""
        raw_json = config.get("session_subscriptions_json", None)
        if isinstance(raw_json, str):
            try:
                value = json.loads(raw_json.strip() or "{}")
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                return {}

        old_value = config.get("session_subscriptions", {})
        return old_value if isinstance(old_value, dict) else {}

    def _save_session_subscriptions(self):
        """保存会话订阅配置。"""
        self.config["session_subscriptions_json"] = json.dumps(
            self.cfg_session_subs,
            ensure_ascii=False,
        )
        self.config.save_config()
