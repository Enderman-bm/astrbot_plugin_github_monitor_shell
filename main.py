import asyncio
import json
import os
from typing import Dict, List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.star import StarTools
from .services.github_service import GitHubService
from .utils.file_utils import write_json_atomic
from .services.group_upload_service import GroupUploadService
from .services.image_render_service import ImageRenderService
from .services.notification_service import NotificationService, format_commit_datetime
from .services.onebot_direct import OneBotDirectSender
from .services.template_manager import TemplateManager
from .utils.target_utils import combine_target_keys, dedupe_targets
from .utils.text_utils import split_long_message


class GitHubMonitorPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        # 单条消息安全长度（防止 QQ 消息过长导致发送失败）
        self.message_max_length = int(self.config.get("message_max_length", 2000) or 2000)
        self.github_service = GitHubService(self.config.get("github_token", ""))
        # commit 变更统计（文件数 / 增删行数）：需要额外的单提交详情请求，故受开关与数量上限控制
        commit_stats_cfg = self.config.get("commit_stats", {}) or {}
        self.commit_stats_enabled = bool(commit_stats_cfg.get("enabled", True))
        try:
            self.commit_stats_max_commits = max(0, int(commit_stats_cfg.get("max_commits", 10)))
        except (TypeError, ValueError):
            self.commit_stats_max_commits = 10
        # 单次检查最多收集的提交数（分页拉取，防止两次轮询之间提交过多时静默丢失）
        commit_fetch_cfg = self.config.get("commit_fetch", {}) or {}
        try:
            self.commit_fetch_max_commits = max(1, int(commit_fetch_cfg.get("max_commits", 50)))
        except (TypeError, ValueError):
            self.commit_fetch_max_commits = 50
        plugin_data_dir = StarTools.get_data_dir("GitHub监控插件")
        # 文转图相关服务（模板目录：插件内置 templates/ + 数据目录自定义 templates/）
        template_manager = TemplateManager(
            builtin_dir=os.path.join(os.path.dirname(__file__), "templates"),
            custom_dir=os.path.join(plugin_data_dir, "templates"),
        )
        image_service = ImageRenderService(
            template_manager, self.html_render, self.config
        )
        upload_service = GroupUploadService(context, self.config)
        image_cfg = self.config.get("image_output", {}) or {}
        onebot_sender = OneBotDirectSender(
            context, enable_base64=bool(image_cfg.get("enable_base64_image", True))
        )
        self.notification_service = NotificationService(
            context, self.config, image_service=image_service,
            upload_service=upload_service, onebot_sender=onebot_sender,
        )
        self.data_file = os.path.join(plugin_data_dir, "commits.json")
        self.sent_notifications_file = os.path.join(plugin_data_dir, "sent_notifications.json")
        self.repo_issues_state_file = os.path.join(plugin_data_dir, "repo_issues_state.json")
        self.monitoring_started = False  # 添加标志以跟踪监控是否已启动
        self._monitor_task: asyncio.Task | None = None
        self._ensure_data_dir()
        self._start_monitoring()

    def _ensure_data_dir(self):
        """确保数据目录存在"""
        data_dir = os.path.dirname(self.data_file)
        os.makedirs(data_dir, exist_ok=True)

    def _load_commit_data(self) -> Dict:
        """加载commit数据"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载commit数据失败: {str(e)}")
            return {}

    def _save_commit_data(self, data: Dict):
        """保存commit数据"""
        try:
            write_json_atomic(self.data_file, data)
        except Exception as e:
            logger.error(f"保存commit数据失败: {str(e)}")

    def _load_sent_notifications(self) -> Dict:
        """加载已发送通知记录"""
        try:
            if os.path.exists(self.sent_notifications_file):
                with open(self.sent_notifications_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载已发送通知记录失败: {str(e)}")
            return {}

    def _save_sent_notifications(self, data: Dict):
        """保存已发送通知记录"""
        try:
            write_json_atomic(self.sent_notifications_file, data)
        except Exception as e:
            logger.error(f"保存已发送通知记录失败: {str(e)}")

    def _load_repo_issues_state(self) -> Dict:
        """加载项目仓库 Issues 动态监控状态

        结构: { "owner/repo": { "issue_number": {
            "title": ..., "author": ..., "url": ...,
            "labels": [...], "updated_at": ..., "comments": n, "last_comment_at": ...
        } } }
        """
        try:
            if os.path.exists(self.repo_issues_state_file):
                with open(self.repo_issues_state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"加载项目仓库 Issues 状态失败: {str(e)}")
            return {}

    def _save_repo_issues_state(self, data: Dict):
        """保存项目仓库 Issues 动态监控状态"""
        try:
            write_json_atomic(self.repo_issues_state_file, data)
        except Exception as e:
            logger.error(f"保存项目仓库 Issues 状态失败: {str(e)}")

    def _parse_repository_config(self, repo_config) -> Dict | None:
        """把仓库配置统一解析为内部结构

        字符串格式: "owner/repo|群1|群2"
        字典格式: {"owner", "repo", "branch", "groups", "notification_targets", "exclusive"}
        """
        if isinstance(repo_config, str):
            parts = [part.strip() for part in repo_config.split("|")]
            repo_path = parts[0]
            if "/" not in repo_path:
                return None
            owner, repo = repo_path.split("/", 1)
            return {
                "owner": owner.strip(),
                "repo": repo.strip(),
                "branch": None,
                "groups": dedupe_targets(parts[1:]),
                "notification_targets": [],
                "exclusive": False,
            }

        if isinstance(repo_config, dict):
            owner = str(repo_config.get("owner") or "").strip()
            repo = str(repo_config.get("repo") or "").strip()
            branch = repo_config.get("branch")
            branch = str(branch).strip() if branch else None
            exclusive_raw = repo_config.get("exclusive", False)
            if isinstance(exclusive_raw, str):
                exclusive = exclusive_raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                exclusive = bool(exclusive_raw)
            return {
                "owner": owner,
                "repo": repo,
                "branch": branch or None,
                "groups": dedupe_targets(repo_config.get("groups", [])),
                # notification_targets 为推荐字段；private_targets 作为兼容别名
                "notification_targets": dedupe_targets(
                    repo_config.get("notification_targets",
                                    repo_config.get("private_targets", []))
                ),
                "exclusive": exclusive,
            }

        return None

    def _resolve_repo_targets(self, repo_cfg: Dict) -> tuple[List[str], List[str]]:
        """计算单个仓库最终生效的 (私聊目标, 群聊目标)

        ``exclusive=True`` 时不继承全局通知目标，只发给该仓库自己的目标；
        默认 False 时维持旧行为：全局目标 + 仓库专属目标并集。
        """
        exclusive = bool(repo_cfg.get("exclusive", False))
        global_private = [] if exclusive else dedupe_targets(
            self.config.get("notification_targets", [])
        )
        global_groups = [] if exclusive else dedupe_targets(
            self.config.get("group_notification_targets", [])
        )
        private_targets = dedupe_targets(
            global_private + dedupe_targets(repo_cfg.get("notification_targets", []))
        )
        group_targets = dedupe_targets(
            global_groups + dedupe_targets(repo_cfg.get("groups", []))
        )
        return private_targets, group_targets

    def _is_commit_already_notified(self, repo_key: str, commit_sha: str, targets: List[str]) -> bool:
        """检查commit是否已经发送过通知给这些目标

        targets 为 combine_target_keys() 生成的组合键（私聊带 dm: 前缀）。
        """
        sent_data = self._load_sent_notifications()
        repo_data = sent_data.get(repo_key, {})
        commit_data = repo_data.get(commit_sha, [])
        target_set = set(dedupe_targets(targets))
        return any(target_set <= set(dedupe_targets(record)) for record in commit_data)

    def _mark_commit_as_notified(self, repo_key: str, commit_sha: str, targets: List[str]):
        """标记commit已发送通知"""
        sent_data = self._load_sent_notifications()
        if repo_key not in sent_data:
            sent_data[repo_key] = {}
        if commit_sha not in sent_data[repo_key]:
            sent_data[repo_key][commit_sha] = []
        combined = dedupe_targets(targets)
        if combined not in sent_data[repo_key][commit_sha]:
            sent_data[repo_key][commit_sha].append(combined)
        self._save_sent_notifications(sent_data)

    def _start_monitoring(self):
        """启动监控任务"""
        # 只启动一次监控任务
        if not self.monitoring_started:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            self.monitoring_started = True
            logger.info("GitHub 监控任务已启动")

    async def terminate(self):
        # 取消 commit 监控任务
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"终止监控任务时出错: {str(e)}")
        self.monitoring_started = False
        self._monitor_task = None

    async def _monitor_loop(self):
        """监控循环"""
        while True:
            try:
                await self._check_repositories()
                # 定期重试失败的通知
                await self.notification_service.retry_failed_notifications()
                await asyncio.sleep(self.config.get("check_interval", 60) * 60)
            except Exception as e:
                logger.error(f"监控循环出错: {str(e)}")
                await asyncio.sleep(60)  # 出错时等待1分钟再重试

    async def _check_repositories(self):
        """检查所有仓库的更新"""
        repositories = self.config.get("repositories", [])
        if not repositories:
            return

        commit_data = self._load_commit_data()
        updated_count = 0

        # 创建当前配置中的仓库键集合，用于清理已删除的仓库数据
        configured_repo_keys = set()

        for repo_config in repositories:
            repo_cfg = self._parse_repository_config(repo_config)
            if not repo_cfg:
                logger.warning(f"无效的仓库配置: {repo_config}")
                continue

            owner = repo_cfg["owner"]
            repo = repo_cfg["repo"]
            branch = repo_cfg["branch"]
            if not owner or not repo:
                logger.warning(f"仓库配置缺少owner或repo: {repo_config}")
                continue

            # 获取仓库信息以确定实际分支
            repo_info = await self.github_service.get_repository_info(owner, repo)
            if not repo_info:
                logger.warning(f"无法获取仓库信息: {owner}/{repo}")
                continue

            default_branch = repo_info.get("default_branch", "main") or "main"
            actual_branch = branch or default_branch
            repo_key = f"{owner}/{repo}/{actual_branch}"
            configured_repo_keys.add(repo_key)

            # 按仓库解析最终生效的推送目标（exclusive=True 时不再叠加全局目标）
            private_targets, group_targets = self._resolve_repo_targets(repo_cfg)
            all_targets = combine_target_keys(private_targets, group_targets)

            # Issues 增量监控：与 Commit 同轮检测，推送目标遵循仓库分流
            if self.config.get("issue_monitor_enabled", True):
                try:
                    await self._check_repo_issues(
                        owner, repo, private_targets, group_targets,
                        repo_info=repo_info, branch=actual_branch,
                    )
                except Exception as e:
                    logger.error(f"检查仓库 {owner}/{repo} 的 Issues 失败: {str(e)}")

            # 获取最新commit
            new_commit = await self.github_service.get_latest_commit(owner, repo, branch)
            if not new_commit:
                continue

            old_commit = commit_data.get(repo_key)

            # 检查是否有变化
            if not old_commit or old_commit.get("sha") != new_commit["sha"]:
                updated_count += 1
                logger.info(f"检测到仓库 {repo_key} 有新的commit: {new_commit['sha'][:7]}")

                # 获取所有新的提交
                new_commits = [new_commit]  # 默认至少包含最新提交
                truncated = False
                if old_commit and old_commit.get("sha"):
                    # 分页获取从上次记录的提交之后的所有提交
                    commits_since = await self.github_service.get_commits_since(
                        owner, repo, old_commit.get("sha"), branch,
                        max_commits=self.commit_fetch_max_commits,
                    )
                    if commits_since is None:
                        # API调用失败，跳过此仓库，但保留旧数据不变
                        continue
                    if commits_since:
                        new_commits = commits_since
                        truncated = bool(getattr(commits_since, "truncated", False))

                # 发送通知 (只有在确实有新提交时才发送)
                if repo_info and new_commits:
                    # 检查是否已经发送过通知
                    latest_sha = new_commits[0]["sha"]
                    if self._is_commit_already_notified(repo_key, latest_sha, all_targets):
                        logger.info(f"仓库 {repo_key} 的提交 {latest_sha[:7]} 已经发送过通知，跳过")
                    else:
                        # 补全变更统计（文件数 / 增删行数），仅在真正要推送时调用，避免浪费 API 配额
                        if self.commit_stats_enabled and self.commit_stats_max_commits > 0:
                            await self.github_service.enrich_commit_stats(
                                owner, repo, new_commits, self.commit_stats_max_commits
                            )

                        # 合并转发模式：本轮只收集，待所有仓库检查完成后统一整合发送
                        if self.notification_service.forward_merge_enabled:
                            self.notification_service.queue_commit_notification(
                                repo_info, new_commits, private_targets, group_targets,
                                branch=actual_branch, repo_key=repo_key,
                                truncated=truncated,
                            )
                            logger.info(
                                f"仓库 {repo_key} 的提交 {latest_sha[:7]} 已加入合并转发队列"
                            )
                        else:
                            # 发送通知
                            failed_private, failed_groups = await self.notification_service.send_commit_notification(
                                repo_info, new_commits, private_targets, group_targets,
                                branch=actual_branch, truncated=truncated,
                            )
                            # 只标记发送成功的目标；失败目标已进入待重试队列
                            failed_private_set = set(failed_private)
                            failed_group_set = set(failed_groups)
                            successful_targets = combine_target_keys(
                                [t for t in private_targets if t not in failed_private_set],
                                [g for g in group_targets if g not in failed_group_set],
                            )
                            if successful_targets:
                                self._mark_commit_as_notified(
                                    repo_key, latest_sha, successful_targets
                                )
                                logger.info(
                                    f"已标记仓库 {repo_key} 的提交 {latest_sha[:7]} "
                                    f"为已通知（成功目标 {len(successful_targets)} 个）"
                                )

                # 更新数据
                commit_data[repo_key] = new_commit  # 仍然只保存最新的提交SHA用于比较
                self._save_commit_data(commit_data)
            else:
                logger.info(f"仓库 {repo_key} 无新commit（最新 {new_commit['sha'][:7]}）")

        # 清理已删除仓库的数据
        removed_keys = set(commit_data.keys()) - configured_repo_keys
        for removed_key in removed_keys:
            del commit_data[removed_key]
            logger.info(f"已清理已删除仓库的数据: {removed_key}")
        if removed_keys:
            self._save_commit_data(commit_data)

        # 合并转发模式：把本轮收集到的 commit 通知整合成转发聊天记录发送，
        # 并按发送结果标记提交为已通知（失败目标已进入待重试队列）
        if self.notification_service.forward_merge_enabled:
            await self._flush_forward_merge_notifications()

        logger.info(
            f"本轮检查完成：共 {len(configured_repo_keys)} 个仓库，{updated_count} 个有更新"
        )

    async def _flush_forward_merge_notifications(self):
        """把本轮收集到的 commit 通知整合成合并转发消息发送并标记已通知

        无论部分目标是否发送失败，出队的提交都会被标记为已通知——失败的目标
        已写入待重试队列，避免下一轮轮询重复收集导致重复推送。

        队列本身在发送前已清空并落盘（见 NotificationService.flush_commit_notifications），
        因此恢复出来的条目即使已经用最新 SHA 标记过，也必须照常发送，否则通知永久丢失。
        """
        try:
            flushed_items = await self.notification_service.flush_commit_notifications()
        except Exception as e:
            logger.error(f"整合发送合并转发 commit 通知失败: {str(e)}", exc_info=True)
            return

        for item in flushed_items:
            repo_key = item.get("repo_key")
            new_commits = item.get("new_commits") or []
            latest_sha = new_commits[0].get("sha", "") if new_commits else ""
            if not repo_key or not latest_sha:
                continue
            failed_private = set(item.get("failed_targets") or [])
            failed_groups = set(item.get("failed_group_targets") or [])
            successful_targets = combine_target_keys(
                [t for t in (item.get("targets") or []) if t not in failed_private],
                [g for g in (item.get("group_targets") or []) if g not in failed_groups],
            )
            # 已通知只代表「已推送过」，不代表「通知已发出」：上一轮整合发送中途异常时，
            # 提交数据可能已推进到该 SHA 而通知没发出去，恢复的条目必须照常发送。
            if successful_targets:
                self._mark_commit_as_notified(repo_key, latest_sha, successful_targets)
                logger.info(
                    f"已标记仓库 {repo_key} 的提交 {latest_sha[:7]} "
                    f"为已通知（合并转发，成功目标 {len(successful_targets)} 个）"
                )

    async def _check_repo_issues(self, owner: str, repo: str,
                                 private_targets: List[str] = None,
                                 group_targets: List[str] = None,
                                 repo_info: Dict = None,
                                 branch: str = None):
        """检测单个项目仓库的 Issues 增量动态

        - 新增 Issue、标题/标签变化、新评论、Issue 关闭时推送增量通知
        - 每仓库每轮汇总为一张文本卡或图片卡，推送目标遵循仓库级分流
        - 首次监控某仓库时仅建立基线快照，不推送，避免刷屏
        """
        private_targets = private_targets or []
        group_targets = group_targets or []
        if repo_info is None:
            repo_info = {
                "owner": {"login": owner},
                "name": repo,
                "html_url": f"https://github.com/{owner}/{repo}",
            }

        repo_name = f"{owner}/{repo}"
        issues = await self.github_service.get_open_issues(owner, repo)
        if issues is None:
            logger.warning(f"Issues 增量监控：获取 {repo_name} 的 issues 失败，本轮跳过")
            return
        issues_truncated = bool(getattr(issues, "truncated", False))

        state = self._load_repo_issues_state()
        old_repo_state = state.get(repo_name)
        first_run = old_repo_state is None  # 该仓库首次纳入监控，只建基线不推送

        new_snapshot = {}
        new_issues = []       # 新增的 issue
        updated_issues = []   # 有动态的 issue，元素: {"issue": {...}, "new_comments": [...], "changes": [...]}

        for issue in issues:
            num = str(issue["number"])
            entry = {
                "title": issue["title"],
                "author": issue["author"],
                "url": issue["url"],
                "labels": issue["labels"],
                "updated_at": issue["updated_at"],
                "comments": issue.get("comments", 0),
            }
            new_snapshot[num] = entry

            if first_run:
                continue

            old = old_repo_state.get(num)
            if old is None:
                new_issues.append({**entry, "number": issue["number"]})
                continue

            comment_added = (
                int(issue.get("comments", 0) or 0)
                > int(old.get("comments", 0) or 0)
            )
            content_changed = issue["updated_at"] != old.get("updated_at", "")
            if not comment_added and not content_changed:
                continue

            # 可读的状态进度：标题 / 标签 / 评论数变化
            changes = []
            if old.get("title") != entry["title"]:
                changes.append(f"标题: {old.get('title') or ''} → {entry['title']}")
            old_labels = old.get("labels") or []
            new_labels = entry["labels"] or []
            if old_labels != new_labels:
                changes.append(
                    f"标签: {', '.join(old_labels) or '无'} → "
                    f"{', '.join(new_labels) or '无'}"
                )
            old_comments = int(old.get("comments", 0) or 0)
            new_comments_count = int(entry.get("comments", 0) or 0)
            if not comment_added and old_comments != new_comments_count:
                changes.append(f"评论: {old_comments} → {new_comments_count}")

            # 有新评论时拉取评论详情，筛选出上次记录之后的新评论
            new_comments = []
            if comment_added:
                comments = await self.github_service.get_issue_comments(owner, repo, issue["number"])
                if comments:
                    last_seen = old.get("last_comment_at", "")
                    new_comments = [
                        c for c in comments
                        if not last_seen or c["created_at"] > last_seen
                    ]
                    latest_time = max(
                        [c["created_at"] for c in new_comments] +
                        ([last_seen] if last_seen else []) +
                        [comments[0]["created_at"]]
                    )
                    # entry 已被 new_snapshot 引用，这里更新会同步写入快照
                    entry["last_comment_at"] = latest_time

            updated_issues.append({
                "issue": {**entry, "number": issue["number"]},
                "new_comments": new_comments,
                "changes": changes,
            })

        # 上次快照中存在、本次 open 列表中消失的 issue 视为已关闭。
        # open issues 被分页上限截断时不能做关闭检测，否则第 N 页之后的
        # Issue 会被误判为已关闭并刷屏。
        closed_issues = []
        if not first_run and old_repo_state:
            if issues_truncated:
                logger.warning(
                    f"Issues 增量监控：{repo_name} 的 open issues 超过分页上限，"
                    "本轮跳过关闭检测以避免误报"
                )
            else:
                for num, old in old_repo_state.items():
                    if num not in new_snapshot:
                        closed_issues.append({**old, "number": int(num)})

        # 更新快照（不在 open 列表中的 issue 视为已关闭，自动移除）
        state[repo_name] = new_snapshot
        self._save_repo_issues_state(state)

        if first_run:
            logger.info(
                f"Issues 增量监控：已为 {repo_name} 建立基线快照"
                f"（{len(new_snapshot)} 个 open issues），本次不推送"
            )
            return

        if not new_issues and not updated_issues and not closed_issues:
            logger.info(f"Issues 增量监控：{repo_name} 无变化")
            return

        if not private_targets and not group_targets:
            logger.info(f"Issues 增量监控：{repo_name} 未配置推送目标，跳过发送")
            return

        await self.notification_service.send_issue_notification(
            repo_info, new_issues, updated_issues, closed_issues,
            private_targets, group_targets, branch=branch,
        )

    @filter.command("github_monitor")
    async def monitor_command(self, event: AstrMessageEvent):
        """手动触发监控检查"""
        try:
            await self._check_repositories()
            yield event.plain_result("✅ 已完成GitHub仓库检查")
        except Exception as e:
            logger.error(f"手动检查失败: {str(e)}")
            yield event.plain_result(f"❌ 检查失败: {str(e)}")

    @filter.command("github_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看监控状态"""
        try:
            commit_data = self._load_commit_data()
            repositories = self.config.get("repositories", [])

            message = "📊 GitHub监控状态\n\n"

            for repo_config in repositories:
                repo_cfg = self._parse_repository_config(repo_config)
                if not repo_cfg or not repo_cfg.get("owner") or not repo_cfg.get("repo"):
                    continue
                owner = repo_cfg["owner"]
                repo = repo_cfg["repo"]
                branch = repo_cfg["branch"]
                if not branch:
                    # 如果没有指定分支，获取默认分支
                    repo_info = await self.github_service.get_repository_info(owner, repo)
                    branch = repo_info.get("default_branch", "main") if repo_info else "main"

                repo_key = f"{owner}/{repo}/{branch}"
                commit_info = commit_data.get(repo_key)

                message += f"📁 {repo_key}\n"
                if commit_info:
                    date_str = commit_info.get("date")
                    formatted_date = None
                    if date_str:
                        formatted_date = format_commit_datetime(
                            date_str,
                            self.config.get("time_zone", "Asia/Shanghai"),
                            self.config.get("time_format", "%Y-%m-%d %H:%M:%S"),
                        )

                    message += f"  最新Commit: {commit_info['sha'][:7]}\n"
                    if formatted_date:
                        message += f"  更新时间: {formatted_date}\n"
                    else:
                        message += "  更新时间: 未知\n"
                else:
                    message += "  状态: 未监控到数据\n"
                message += "\n"

            yield event.plain_result(message)

        except Exception as e:
            logger.error(f"获取状态失败: {str(e)}")
            yield event.plain_result(f"❌ 获取状态失败: {str(e)}")

    @filter.command("github_issues")
    async def issues_command(self, event: AstrMessageEvent):
        """查询当前用户所有仓库的 open issues（需要配置 github_token）"""
        try:
            # 检查是否配置了 token
            if not self.config.get("github_token"):
                yield event.plain_result("⚠️ 请先在插件配置中填写 github_token，否则无法获取你的仓库列表")
                return

            # 获取当前认证用户信息
            user = await self.github_service.get_current_user()
            if not user:
                yield event.plain_result("❌ 无法获取用户信息，请检查 github_token 是否有效")
                return

            username = user["login"]

            # 分页获取用户所有仓库（/user/repos 认证接口，含私有仓库，type=owner 不含 fork）
            all_repos = []
            page = 1
            while True:
                repos = await self.github_service.get_user_repos(page=page, per_page=100)
                if repos is None:
                    yield event.plain_result(f"❌ 获取用户 {username} 的仓库列表失败")
                    return
                if not repos:
                    break
                all_repos.extend(repos)
                if len(repos) < 100:
                    break
                page += 1

            if not all_repos:
                yield event.plain_result(f"✅ 用户 {username} 没有任何仓库")
                return

            message = f"📋 {username} 的 Open Issues\n\n"
            total_issues = 0
            repos_with_issues = 0

            for repo in all_repos:
                repo_name = repo["full_name"]

                # 跳过没有 open issues 的仓库（利用 API 返回的计数快速过滤）
                if repo.get("open_issues_count", 0) == 0:
                    continue

                # 获取该仓库的 open issues 详情
                issues = await self.github_service.get_open_issues(repo["owner"]["login"], repo["name"])
                if not issues:
                    continue

                # 确认有 issues 后才计数，避免计数与实际不一致
                repos_with_issues += 1
                message += f"📁 {repo_name}（{len(issues)} 个 open issues）\n"

                for issue in issues:
                    labels_str = ""
                    if issue["labels"]:
                        labels_str = f" 🏷️ {','.join(issue['labels'])}"
                    message += f"  #{issue['number']} {issue['title']}{labels_str}\n"
                    message += f"     👤 {issue['author']} | 🔗 {issue['url']}\n"
                    total_issues += 1

                message += "\n"

            if repos_with_issues == 0:
                yield event.plain_result(f"✅ {username} 的所有仓库均无 open issues（共 {len(all_repos)} 个仓库）")
            else:
                message += f"📊 共 {len(all_repos)} 个仓库，其中 {repos_with_issues} 个仓库有 open issues，共 {total_issues} 个"
                # 仓库/issue 较多时消息可能超过 QQ 单条消息长度限制，分片发送
                for chunk in split_long_message(message, self.message_max_length):
                    yield event.plain_result(chunk)

        except Exception as e:
            logger.error(f"查询 issues 失败: {str(e)}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")
