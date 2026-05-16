# AstrBot GitHub 动态监听插件

通过 GitHub 公开的 RSS/Atom Feed 定时获取用户动态、仓库 Release 和仓库 Commit 信息，并推送到已绑定的聊天会话。

## 功能特性

- 用户动态监听：监听 GitHub 用户公开动态，例如 Star、Fork、创建仓库、Push 等。
- 仓库 Release 监听：监听指定仓库的新版本发布。
- 仓库 Commit 监听：监听指定仓库的最新提交。
- 多会话推送：支持绑定多个聊天会话。
- 会话订阅过滤：不同会话可以订阅不同 GitHub 目标。
- 手动检查：通过 `/gh_check` 立即查看指定用户或仓库的最新动态。
- 时区转换：按配置的时区显示 GitHub 动态时间。

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/gh_list` | 所有人 | 查看当前监听项和绑定会话数量 |
| `/gh_check <用户名>` | 管理员 | 立即查看某个 GitHub 用户的最新动态 |
| `/gh_check <owner/repo>` | 管理员 | 立即查看某个仓库的最新 Release |
| `/gh_check <owner/repo> commit` | 管理员 | 立即查看某个仓库的最新 Commit |
| `/gh_bindhere` | 管理员 | 将当前会话绑定为推送目标 |
| `/gh_unbindhere` | 管理员 | 解绑当前会话，并清除该会话的订阅配置 |
| `/gh_sub <目标>` | 管理员 | 为当前会话订阅指定目标 |
| `/gh_unsub <目标>` | 管理员 | 取消当前会话对指定目标的订阅 |
| `/gh_mysubs` | 所有人 | 查看当前会话的订阅列表 |

订阅目标格式：

```text
user:octocat
repo:microsoft/vscode
commits:microsoft/vscode
```

也可以使用：

```text
/gh_sub all
/gh_unsub all
```

`all` 会清空当前会话的自定义订阅过滤，使该会话恢复为接收全部监听目标的推送。

## 配置项

在 AstrBot WebUI 的插件配置中设置：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `poll_interval` | int | `1800` | 轮询间隔，单位秒，建议不低于 600 秒 |
| `max_entries` | int | `5` | 单次推送每个目标最多显示多少条新动态；设为 `0` 表示不限制 |
| `watch_users` | list | `[]` | 要监听的 GitHub 用户名列表 |
| `watch_repos` | list | `[]` | 要监听 Release 的仓库列表，格式为 `owner/repo` |
| `watch_repos_commits` | list | `[]` | 要监听 Commit 的仓库列表，格式为 `owner/repo` |
| `timezone` | string | `Asia/Shanghai` | 用于显示动态时间的时区 |
| `bound_sessions` | list | `[]` | 推送目标会话 UMO，可用 `/sid` 获取，也可用 `/gh_bindhere` 自动绑定 |
| `session_subscriptions_json` | text | `{}` | 会话订阅映射 JSON，用于控制不同会话接收不同目标 |

## 会话订阅映射 JSON

`session_subscriptions_json` 用 JSON 字符串保存会话订阅关系。键是会话 UMO，值是该会话订阅的目标列表。

示例：

```json
{
  "aiocqhttp:GroupMessage:123456": [
    "repo:microsoft/vscode"
  ],
  "aiocqhttp:GroupMessage:888888": [
    "repo:astrbotdevs/astrbot",
    "commits:astrbotdevs/astrbot"
  ]
}
```

### 不配置会话订阅映射时

如果 `session_subscriptions_json` 保持默认值：

```json
{}
```

所有已绑定会话都会接收所有监听目标的推送。这是默认行为，也兼容旧版本使用方式。

### 同时配置监听目标、绑定会话和订阅映射时

插件会先从 `watch_users`、`watch_repos`、`watch_repos_commits` 中轮询 GitHub 动态，再按会话订阅映射过滤推送。

例如配置：

```text
watch_repos = [
  "microsoft/vscode",
  "AstrBotDevs/AstrBot"
]

bound_sessions = [
  "aiocqhttp:GroupMessage:123456",
  "aiocqhttp:GroupMessage:888888",
  "aiocqhttp:GroupMessage:999999"
]
```

并配置：

```json
{
  "aiocqhttp:GroupMessage:123456": [
    "repo:microsoft/vscode"
  ],
  "aiocqhttp:GroupMessage:888888": [
    "repo:astrbotdevs/astrbot"
  ]
}
```

推送结果：

- `aiocqhttp:GroupMessage:123456` 只接收 `microsoft/vscode` 的 Release 推送。
- `aiocqhttp:GroupMessage:888888` 只接收 `AstrBotDevs/AstrBot` 的 Release 推送。
- `aiocqhttp:GroupMessage:999999` 没有配置订阅过滤，因此接收全部 Release 推送。

注意：订阅目标会按小写匹配，所以 `AstrBotDevs/AstrBot` 对应的订阅 ID 是：

```text
repo:astrbotdevs/astrbot
```

## 快速开始

1. 在 WebUI 中配置要监听的用户或仓库。
2. 在目标聊天会话中发送 `/gh_bindhere` 绑定当前会话。
3. 如需不同会话接收不同内容，使用 `/gh_sub` 和 `/gh_unsub` 管理订阅。
4. 插件会按 `poll_interval` 自动检查并推送新动态。
5. 可随时使用 `/gh_check <目标>` 手动查看最新动态。

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_listen_github` 安装，或手动克隆：

```bash
cd AstrBot/data/plugins
git clone https://github.com/aliveriver/astrbot_plugin_listen_github.git
```

要求：AstrBot >= 4.9.2

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)

## 许可证

[GPL-3.0](LICENSE)
