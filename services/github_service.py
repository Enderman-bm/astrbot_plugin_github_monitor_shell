from typing import Dict, Optional, List

import httpx

from astrbot.api import logger


class CommitBatch(list):
    """提交列表，额外携带本次拉取的截断状态

    继承 list 以保持调用方原有 ``List[Dict]`` 用法不变；``truncated`` 为 True
    表示达到单次收集上限，可能还有更早的提交没有返回。
    """

    def __init__(self, commits=(), truncated: bool = False, limit: int = 0):
        super().__init__(commits)
        self.truncated = bool(truncated)
        self.limit = int(limit or 0)


class IssueBatch(list):
    """Issue 列表，额外携带分页截断状态（用于判断是否可以做关闭检测）"""

    def __init__(self, issues=(), truncated: bool = False, limit: int = 0):
        super().__init__(issues)
        self.truncated = bool(truncated)
        self.limit = int(limit or 0)


class GitHubService:
    def __init__(self, token: str = ""):
        self.token = token
        self.base_url = "https://api.github.com"
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-GitHub-Monitor"
        }
        if token:
            self.headers["Authorization"] = f"token {token}"

    async def get_latest_commit(self, owner: str, repo: str, branch: str | None = None) -> Optional[Dict]:
        """获取指定仓库最新commit信息"""
        try:
            # 如果没有指定分支，则获取默认分支
            if not branch:
                repo_info = await self.get_repository_info(owner, repo)
                if repo_info and "default_branch" in repo_info:
                    branch = repo_info["default_branch"]
                else:
                    logger.error("无法获取默认分支信息")
                    logger.error(f"请检查仓库 {owner}/{repo} 是否存在，或是否有访问权限。")
                    logger.error(f"并确保可以正常访问 GitHub API：https://api.github.com/repos/{owner}/{repo}")
                    return None

            url = f"{self.base_url}/repos/{owner}/{repo}/commits/{branch}"

            logger.info(f"正在获取最新commit信息: {url}")

            # 使用内置证书和禁用SSL验证
            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                # 单提交详情接口自带变更统计，无需额外请求
                return self._to_commit_dict(response.json(), include_stats=True)
            elif response.status_code == 404:
                # 仓库不存在或分支不存在
                logger.warning(f"仓库或分支不存在: {owner}/{repo}/{branch}")
                return None
            elif response.status_code == 403:
                # API限制或其他权限问题
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取commit失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    async def get_commits_since(self, owner: str, repo: str, since_sha: str,
                                branch: str | None = None,
                                max_commits: int = 50) -> Optional[List[Dict]]:
        """分页获取从指定 commit 之后的所有提交（不含 since_sha 本身）

        GitHub 单页最多 100 条，旧实现固定 ``per_page=10`` 且只取第一页，
        两次轮询之间超过 10 个提交时会静默丢失更早的提交。这里按页拉取，
        直到遇到 ``since_sha``、没有下一页或达到 ``max_commits`` 上限。

        Returns:
            ``CommitBatch``（list 子类）。达到上限仍未遇到 ``since_sha`` 时
            ``truncated`` 为 True，调用方可在通知中提示“更早提交未展示”。
        """
        try:
            # 如果没有指定分支，则获取默认分支
            if not branch:
                repo_info = await self.get_repository_info(owner, repo)
                if repo_info and "default_branch" in repo_info:
                    branch = repo_info["default_branch"]
                else:
                    logger.error("无法获取默认分支信息")
                    logger.error(f"请检查仓库 {owner}/{repo} 是否存在，或是否有访问权限。")
                    logger.error(f"并确保可以正常访问 GitHub API：https://api.github.com/repos/{owner}/{repo}")
                    return None

            max_commits = max(1, int(max_commits or 50))
            per_page = min(100, max_commits)
            url = f"{self.base_url}/repos/{owner}/{repo}/commits"

            commits = CommitBatch(limit=max_commits)
            page = 1
            found_since = False

            while True:
                params = {
                    "sha": branch,
                    "per_page": per_page,
                    "page": page,
                }
                logger.info(
                    f"正在获取commit历史信息: {url}（第 {page} 页，"
                    f"per_page={per_page}，最多 {max_commits} 条）"
                )

                # 使用内置证书和禁用SSL验证
                async with httpx.AsyncClient(
                        timeout=30.0,
                        verify=False
                ) as client:
                    response = await client.get(url, headers=self.headers, params=params)

                if response.status_code == 404:
                    # 仓库不存在或分支不存在
                    logger.warning(f"仓库或分支不存在: {owner}/{repo}/{branch}")
                    return None
                elif response.status_code == 403:
                    # API限制或其他权限问题
                    logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                    return None
                elif response.status_code != 200:
                    logger.error(f"获取commit历史失败: {response.status_code} - {response.text}")
                    return None

                page_data = response.json()
                if not isinstance(page_data, list) or not page_data:
                    break

                for commit_data in page_data:
                    if commit_data.get("sha") == since_sha:
                        found_since = True
                        break
                    if len(commits) >= max_commits:
                        commits.truncated = True
                        break
                    commits.append(self._to_commit_dict(commit_data))

                if found_since or commits.truncated:
                    break
                if len(commits) >= max_commits:
                    # 当前页刚好用满额度，是否还有更早的提交由 Link 头决定
                    commits.truncated = self._has_next_page(response)
                    break
                if not self._has_next_page(response):
                    break
                page += 1

            if not found_since and not commits.truncated:
                logger.warning(
                    f"未在 {owner}/{repo} 的提交历史中找到上次记录的提交 "
                    f"{str(since_sha)[:7]}，可能发生了强制推送或分支重写"
                )
            if commits.truncated:
                logger.warning(
                    f"仓库 {owner}/{repo} 两次检查之间提交数超过单次上限 "
                    f"{max_commits}，更早的提交未收集"
                )
            return commits

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    @staticmethod
    def _to_commit_dict(commit_data: Dict, include_stats: bool = False) -> Dict:
        """把 GitHub API 的 commit 响应转换为插件内部结构"""
        commit = {
            "sha": commit_data["sha"],
            "message": commit_data["commit"]["message"],
            "author": commit_data["commit"]["author"]["name"],
            "date": commit_data["commit"]["author"]["date"],
            "url": commit_data["html_url"],
        }
        if include_stats:
            commit["stats"] = GitHubService._extract_commit_stats(commit_data)
        return commit

    @staticmethod
    def _has_next_page(response) -> bool:
        """根据 GitHub 的 Link 响应头判断是否还有下一页"""
        link = response.headers.get("link", "") or ""
        return 'rel="next"' in link or "rel='next'" in link

    @staticmethod
    def _extract_commit_stats(commit_data: Dict) -> Optional[Dict]:
        """从单提交详情响应中提取变更统计

        合并提交、超大 diff 等场景 GitHub 可能不返回 stats/files，此时返回 None，
        由调用方跳过展示（不阻塞通知）。
        """
        stats = commit_data.get("stats")
        if not isinstance(stats, dict):
            return None
        files = commit_data.get("files")
        return {
            "additions": int(stats.get("additions") or 0),
            "deletions": int(stats.get("deletions") or 0),
            "total": int(stats.get("total") or 0),
            "changed_files": len(files) if isinstance(files, list) else None
        }

    async def get_commit_stats(self, owner: str, repo: str, sha: str) -> Optional[Dict]:
        """获取单个 commit 的变更统计（改动文件数、新增/删除行数）

        注意：commit 列表接口不返回统计信息，只能逐个查询单提交详情接口。
        """
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/commits/{sha}"

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return self._extract_commit_stats(response.json())
            elif response.status_code == 404:
                logger.warning(f"commit 不存在: {owner}/{repo}@{sha[:7]}")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取 commit 统计失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求GitHub API失败: {str(e)}")
            return None

    async def enrich_commit_stats(self, owner: str, repo: str, commits: List[Dict],
                                  max_commits: int = 10) -> List[Dict]:
        """为缺少统计信息的 commit 补全变更统计（原地写入 stats 字段）

        已带 stats 的提交不会重复请求；最多补全 ``max_commits`` 个，
        ``max_commits <= 0`` 时不做任何请求。单个提交查询失败不影响其他提交。
        """
        if max_commits <= 0:
            return commits

        queried = 0
        for commit in commits:
            if queried >= max_commits:
                break
            if commit.get("stats"):
                continue
            sha = commit.get("sha")
            if not sha:
                continue
            queried += 1
            stats = await self.get_commit_stats(owner, repo, sha)
            if stats:
                commit["stats"] = stats
        return commits

    async def get_repository_info(self, owner: str, repo: str) -> Optional[Dict]:
        """获取仓库信息"""
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}"

            logger.info(f"正在获取仓库信息: {url}")

            # 使用内置证书和禁用SSL验证
            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                # 仓库不存在
                logger.warning(f"仓库不存在: {owner}/{repo}")
                return None
            elif response.status_code == 403:
                # API限制或其他权限问题
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取仓库信息失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"获取仓库信息失败: {str(e)}")
            return None

    async def get_open_issues(self, owner: str, repo: str,
                              max_issues: int = 500) -> Optional[List[Dict]]:
        """分页获取指定仓库的所有 open issues（最多 max_issues 条）

        GitHub 每页最多 100 条；只取第一页会把第 101 个之后的 Issue 当成
        “已关闭”，造成误报。这里按 Link 头分页，并用 IssueBatch.truncated
        标记是否达到上限，调用方可据此跳过关闭检测。
        """
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/issues"
            max_issues = max(1, int(max_issues or 500))
            per_page = 100
            issues = IssueBatch(limit=max_issues)
            page = 1

            while True:
                params = {
                    "state": "open",
                    "per_page": per_page,
                    "page": page,
                }
                logger.info(
                    f"正在获取 open issues: {url}（第 {page} 页，最多 {max_issues} 条）"
                )

                async with httpx.AsyncClient(
                        timeout=30.0,
                        verify=False
                ) as client:
                    response = await client.get(url, headers=self.headers, params=params)

                if response.status_code == 404:
                    logger.warning(f"仓库不存在或无权限: {owner}/{repo}")
                    return None
                elif response.status_code == 403:
                    logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                    return None
                elif response.status_code != 200:
                    logger.error(f"获取 open issues 失败: {response.status_code} - {response.text}")
                    return None

                issues_data = response.json()
                if not isinstance(issues_data, list) or not issues_data:
                    break

                for issue in issues_data:
                    # 过滤掉 pull requests（GitHub API 会将 PR 也返回）
                    if "pull_request" in issue:
                        continue
                    if len(issues) >= max_issues:
                        issues.truncated = True
                        break
                    issues.append({
                        "number": issue["number"],
                        "title": issue["title"],
                        "author": issue["user"]["login"],
                        "created_at": issue["created_at"],
                        "updated_at": issue["updated_at"],
                        "url": issue["html_url"],
                        "labels": [label["name"] for label in issue.get("labels", [])],
                        "comments": issue.get("comments", 0),
                    })

                if issues.truncated:
                    break
                if not self._has_next_page(response):
                    break
                page += 1

            if issues.truncated:
                logger.warning(
                    f"仓库 {owner}/{repo} 的 open issues 超过单次上限 "
                    f"{max_issues}，已截断"
                )
            return issues

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None

    async def get_issue_comments(self, owner: str, repo: str, issue_number: int) -> Optional[List[Dict]]:
        """获取指定 issue 的最新评论（按时间倒序，最多返回前30条）"""
        try:
            url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
            params = {
                "sort": "created",
                "direction": "desc",
                "per_page": 30
            }

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers, params=params)

            if response.status_code == 200:
                comments_data = response.json()
                return [
                    {
                        "author": c["user"]["login"],
                        "body": (c.get("body") or "").strip(),
                        "created_at": c["created_at"],
                        "url": c["html_url"]
                    }
                    for c in comments_data
                ]
            elif response.status_code == 404:
                logger.warning(f"Issue 或仓库不存在: {owner}/{repo}#{issue_number}")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取 issue 评论失败: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None

    async def get_current_user(self) -> Optional[Dict]:
        """获取当前认证用户的信息（需要 token）"""
        try:
            url = f"{self.base_url}/user"

            logger.info(f"正在获取当前用户信息: {url}")

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                logger.error("认证失败：Token 无效或未配置")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取用户信息失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None

    async def get_user_repos(self, page: int = 1, per_page: int = 100) -> Optional[List[Dict]]:
        """获取当前认证用户的所有仓库（含私有仓库，支持分页）

        使用 /user/repos 认证接口，而非 /users/{username}/repos，
        后者只返回公开仓库，前者能返回包括私有仓库在内的全部仓库。
        """
        try:
            url = f"{self.base_url}/user/repos"
            params = {
                "type": "owner",  # 只获取用户拥有的仓库，不包含 fork 的
                "sort": "updated",
                "per_page": per_page,
                "page": page
            }

            logger.info(f"正在获取当前用户的所有仓库: {url}")

            async with httpx.AsyncClient(
                    timeout=30.0,
                    verify=False
            ) as client:
                response = await client.get(url, headers=self.headers, params=params)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                logger.error("认证失败：Token 无效或未配置")
                return None
            elif response.status_code == 403:
                logger.error(f"访问被拒绝或API限制: {response.status_code} - {response.text}")
                return None
            else:
                logger.error(f"获取用户仓库失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"请求 GitHub API 失败: {str(e)}")
            return None