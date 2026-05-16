from astrbot.api.star import Star, register

try:
    from .github_commands import GitHubCommandMixin
    from .github_rss import GitHubRssMixin
    from .github_state import GitHubStateMixin
except ImportError:
    from github_commands import GitHubCommandMixin
    from github_rss import GitHubRssMixin
    from github_state import GitHubStateMixin


@register(
    "astrbot_plugin_listen_github",
    "aliveriver",
    "通过 RSS 定时获取 GitHub 用户/仓库动态并推送到聊天会话",
    "1.1.0",
    "https://github.com/aliveriver/astrbot_plugin_listen_github",
)
class GitHubListenPlugin(GitHubCommandMixin, GitHubRssMixin, GitHubStateMixin, Star):
    """GitHub 动态监听插件。"""
