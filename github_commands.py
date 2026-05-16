from astrbot.api.event import AstrMessageEvent, filter

try:
    from .github_shared import (
        GITHUB_REPO_COMMITS_FEED,
        GITHUB_REPO_FEED,
        GITHUB_USER_FEED,
        RE_REPO,
        RE_USERNAME,
        format_single_check,
        parse_check_args,
    )
except ImportError:
    from github_shared import (
        GITHUB_REPO_COMMITS_FEED,
        GITHUB_REPO_FEED,
        GITHUB_USER_FEED,
        RE_REPO,
        RE_USERNAME,
        format_single_check,
        parse_check_args,
    )


class GitHubCommandMixin:
    """负责全部指令处理。"""

    @filter.command("gh_list")
    async def gh_list(self, event: AstrMessageEvent):
        """列出当前监听配置。"""
        lines = ["GitHub 动态监听列表：\n"]
        if self.cfg_watch_users:
            for user in self.cfg_watch_users:
                lines.append(f"  用户：{user}")
        if self.cfg_watch_repos:
            for repo in self.cfg_watch_repos:
                lines.append(f"  仓库：{repo}（Release）")
        if self.cfg_watch_repos_commits:
            for repo in self.cfg_watch_repos_commits:
                lines.append(f"  仓库：{repo}（Commit）")
        if not self.cfg_watch_users and not self.cfg_watch_repos and not self.cfg_watch_repos_commits:
            lines.append("暂无监听项，请在 WebUI 配置中设置。")
        else:
            lines.append(f"\n已绑定会话：{len(self.cfg_bound_sessions)} 个")
        lines.append(f"轮询间隔：{self.poll_interval} 秒")
        yield event.plain_result("\n".join(lines))

    @filter.command("gh_check")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_check(self, event: AstrMessageEvent):
        """立即检查指定 GitHub 目标的最新动态。"""
        parts = parse_check_args(event.message_str)
        if not parts:
            yield event.plain_result(
                "请提供目标，例如：\n"
                "  /gh_check torvalds\n"
                "  /gh_check microsoft/vscode\n"
                "  /gh_check microsoft/vscode commit"
            )
            return

        target = parts[0]
        check_type = parts[1].lower() if len(parts) > 1 else None

        if "/" in target:
            if not RE_REPO.match(target):
                yield event.plain_result("仓库格式不正确，应为 owner/repo，例如 microsoft/vscode")
                return
        elif not RE_USERNAME.match(target):
            yield event.plain_result("用户名格式不正确，仅支持字母、数字、点、连字符和下划线。")
            return

        if "/" in target:
            if check_type == "commit":
                feed_url = GITHUB_REPO_COMMITS_FEED.format(repo=target)
                display_name = f"{target}（Commit）"
            else:
                feed_url = GITHUB_REPO_FEED.format(repo=target)
                display_name = f"{target}（Release）"
        else:
            feed_url = GITHUB_USER_FEED.format(username=target)
            display_name = target

        yield event.plain_result(f"正在获取 {display_name} 的最新动态……")

        feed = await self._fetch_feed(feed_url)
        if feed is None or not feed.entries:
            yield event.plain_result(f"无法获取 {display_name} 的动态，请检查名称是否正确。")
            return

        entries = []
        check = feed.entries if self.max_entries == 0 else feed.entries[: self.max_entries]
        for entry in check:
            entries.append({
                "title": entry.get("title", "无标题").strip(),
                "link": entry.get("link", "").strip(),
                "published": self._convert_time(entry.get("published") or entry.get("updated", "")),
                "content": self._extract_content(entry),
            })

        yield event.plain_result(format_single_check(display_name, entries))

    @filter.command("gh_sub")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_sub(self, event: AstrMessageEvent):
        """为当前会话订阅指定目标。"""
        umo = event.unified_msg_origin
        parts = event.message_str.strip().split()
        args = parts[1:] if len(parts) > 1 and parts[0].lower().rstrip("@") in {"/gh_sub", "gh_sub"} else parts

        if not args:
            yield event.plain_result(
                "请提供订阅目标，例如：\n"
                "  /gh_sub user:octocat\n"
                "  /gh_sub repo:microsoft/vscode\n"
                "  /gh_sub commits:microsoft/vscode\n"
                "  /gh_sub all"
            )
            return

        if umo not in self.cfg_bound_sessions:
            yield event.plain_result("当前会话尚未绑定，请先使用 /gh_bindhere 绑定。")
            return

        async with self._config_lock:
            current_subs = list(self.cfg_session_subs.get(umo, []))

            if args[0].lower() == "all":
                self.cfg_session_subs.pop(umo, None)
                self._save_session_subscriptions()
                yield event.plain_result("已设置为接收全部目标的推送。")
                return

            all_valid_ids = self._get_all_sub_ids()
            added = []
            invalid = []
            already = []

            for arg in args:
                sub_id = self._parse_sub_id(arg)
                if not sub_id:
                    invalid.append(arg)
                    continue
                if sub_id not in all_valid_ids:
                    invalid.append(f"{arg}（未在监听列表中）")
                    continue
                if sub_id in current_subs:
                    already.append(sub_id)
                    continue
                current_subs.append(sub_id)
                added.append(sub_id)

            if added:
                self.cfg_session_subs[umo] = current_subs
                self._save_session_subscriptions()

        lines = []
        if added:
            lines.append(f"已订阅：{', '.join(added)}")
        if already:
            lines.append(f"已存在：{', '.join(already)}")
        if invalid:
            lines.append(f"无效目标：{', '.join(invalid)}")
        lines.append(f"\n当前订阅 {len(current_subs)} 个目标。")
        yield event.plain_result("\n".join(lines))

    @filter.command("gh_unsub")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_unsub(self, event: AstrMessageEvent):
        """取消当前会话对指定目标的订阅。"""
        umo = event.unified_msg_origin
        parts = event.message_str.strip().split()
        args = parts[1:] if len(parts) > 1 and parts[0].lower().rstrip("@") in {"/gh_unsub", "gh_unsub"} else parts

        if not args:
            yield event.plain_result(
                "请提供要取消的目标，例如：\n"
                "  /gh_unsub user:octocat\n"
                "  /gh_unsub repo:microsoft/vscode\n"
                "  /gh_unsub all"
            )
            return

        if umo not in self.cfg_bound_sessions:
            yield event.plain_result("当前会话尚未绑定。")
            return

        async with self._config_lock:
            current_subs = list(self.cfg_session_subs.get(umo, []))

            if args[0].lower() == "all":
                self.cfg_session_subs.pop(umo, None)
                self._save_session_subscriptions()
                yield event.plain_result("已清空订阅列表，将接收全部目标的推送。")
                return

            removed = []
            not_found = []

            for arg in args:
                sub_id = self._parse_sub_id(arg)
                if not sub_id:
                    not_found.append(arg)
                    continue
                if sub_id in current_subs:
                    current_subs.remove(sub_id)
                    removed.append(sub_id)
                else:
                    not_found.append(arg)

            if removed:
                if current_subs:
                    self.cfg_session_subs[umo] = current_subs
                else:
                    self.cfg_session_subs.pop(umo, None)
                self._save_session_subscriptions()

        lines = []
        if removed:
            lines.append(f"已取消订阅：{', '.join(removed)}")
        if not_found:
            lines.append(f"未找到：{', '.join(not_found)}")
        if current_subs:
            lines.append(f"\n剩余订阅 {len(current_subs)} 个目标。")
        else:
            lines.append("\n当前没有自定义订阅，将接收全部目标的推送。")
        yield event.plain_result("\n".join(lines))

    @filter.command("gh_mysubs")
    async def gh_mysubs(self, event: AstrMessageEvent):
        """查看当前会话的订阅列表。"""
        umo = event.unified_msg_origin
        if umo not in self.cfg_bound_sessions:
            yield event.plain_result("当前会话尚未绑定，请先使用 /gh_bindhere 绑定。")
            return

        subs = self.cfg_session_subs.get(umo)
        if not subs:
            yield event.plain_result("当前会话未设置自定义订阅，将接收全部目标的推送。\n使用 /gh_sub 可订阅指定目标。")
            return

        lines = ["当前会话的订阅列表：\n"]
        for sub_id in subs:
            if sub_id.startswith("user:"):
                lines.append(f"  用户：{sub_id}")
            elif sub_id.startswith("repo:"):
                lines.append(f"  仓库：{sub_id}")
            elif sub_id.startswith("commits:"):
                lines.append(f"  提交：{sub_id}")
            else:
                lines.append(f"  {sub_id}")
        lines.append(f"\n共 {len(subs)} 个订阅目标。")
        lines.append("使用 /gh_unsub all 可清空订阅并恢复接收全部推送。")
        yield event.plain_result("\n".join(lines))

    @filter.command("gh_bindhere")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_bindhere(self, event: AstrMessageEvent):
        """将当前会话绑定为推送目标。"""
        umo = event.unified_msg_origin
        async with self._config_lock:
            if umo in self.cfg_bound_sessions:
                yield event.plain_result("当前会话已在绑定列表中。")
                return
            self.cfg_bound_sessions.append(umo)
            self.config["bound_sessions"] = self.cfg_bound_sessions
            self.config.save_config()
        yield event.plain_result(
            f"已绑定当前会话。\n"
            f"会话标识：{umo}\n"
            f"当前共有 {len(self.cfg_bound_sessions)} 个绑定会话。"
        )

    @filter.command("gh_unbindhere")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gh_unbindhere(self, event: AstrMessageEvent):
        """解绑当前会话。"""
        umo = event.unified_msg_origin
        async with self._config_lock:
            if umo not in self.cfg_bound_sessions:
                yield event.plain_result("当前会话不在绑定列表中。")
                return
            self.cfg_bound_sessions.remove(umo)
            self.config["bound_sessions"] = self.cfg_bound_sessions
            self.cfg_session_subs.pop(umo, None)
            self._save_session_subscriptions()
        yield event.plain_result(
            f"已解绑当前会话，并清除对应订阅配置。\n"
            f"剩余 {len(self.cfg_bound_sessions)} 个绑定会话。"
        )
