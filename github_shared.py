import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

GITHUB_USER_FEED = "https://github.com/{username}.atom"
GITHUB_REPO_FEED = "https://github.com/{repo}/releases.atom"
GITHUB_REPO_COMMITS_FEED = "https://github.com/{repo}/commits.atom"
KV_LAST_ENTRY_PREFIX = "last_entry_"
KV_INITIALIZED_PREFIX = "initialized_"
MAX_SCAN_ENTRIES = 50

RE_USERNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?$")
RE_REPO = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


def normalize_watch_list(items: Iterable[str], item_type: str, logger=None) -> List[str]:
    """规范化监听列表，去空、去重并过滤非法项。"""
    normalized: List[str] = []
    seen = set()
    for item in items or []:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value:
            continue
        is_valid = RE_REPO.match(value) if item_type == "repo" else RE_USERNAME.match(value)
        if not is_valid:
            if logger:
                logger.warning(f"[GitHub Listen] 跳过非法监听项({item_type}): {item}")
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


def parse_check_args(message_text: str) -> List[str]:
    """解析 /gh_check 参数，兼容消息中仍带命令前缀的情况。"""
    tokens = message_text.strip().split()
    if not tokens:
        return []
    cmd = tokens[0].split("@", 1)[0].lower()
    if cmd in {"/gh_check", "gh_check"}:
        return tokens[1:]
    return tokens


def parse_sub_id(sub_id: str) -> Optional[str]:
    """解析订阅标识，返回规范化结果；格式非法时返回 None。"""
    sub_id = sub_id.strip().lower()
    if sub_id.startswith("user:"):
        name = sub_id[5:]
        if RE_USERNAME.match(name):
            return f"user:{name}"
    elif sub_id.startswith("repo:"):
        repo = sub_id[5:]
        if RE_REPO.match(repo):
            return f"repo:{repo}"
    elif sub_id.startswith("commits:"):
        repo = sub_id[8:]
        if RE_REPO.match(repo):
            return f"commits:{repo}"
    return None


def convert_time(time_str: str, timezone_name: str) -> str:
    """把 GitHub 时间转换为指定时区的本地时间字符串。"""
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
        return dt.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return time_str


def extract_content(entry) -> str:
    """提取并清理 RSS 条目正文。"""
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


def format_entries(display_name: str, entries: List[dict]) -> str:
    """格式化轮询推送消息。"""
    lines = [f"【{display_name}】共有 {len(entries)} 条新动态：\n"]
    for i, entry in enumerate(entries, 1):
        lines.append(f"  {i}. {entry['title']}")
        if entry["published"]:
            lines.append(f"     时间：{entry['published']}")
        if entry["content"]:
            lines.append(f"     内容：{entry['content']}")
        if entry["link"]:
            lines.append(f"     链接：{entry['link']}")
        lines.append("")
    return "\n".join(lines)


def format_single_check(display_name: str, entries: List[dict]) -> str:
    """格式化 /gh_check 返回消息。"""
    if not entries:
        return f"【{display_name}】暂无最新公开动态。"
    lines = [f"【{display_name}】最近的动态：\n"]
    for i, entry in enumerate(entries, 1):
        lines.append(f"  {i}. {entry['title']}")
        if entry["published"]:
            lines.append(f"     时间：{entry['published']}")
        if entry["content"]:
            lines.append(f"     内容：{entry['content']}")
        if entry["link"]:
            lines.append(f"     链接：{entry['link']}")
        lines.append("")
    return "\n".join(lines)
