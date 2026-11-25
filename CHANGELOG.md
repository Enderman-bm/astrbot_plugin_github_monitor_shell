# Changelog

所有 noteworthy 的插件更新都会记录在此文件中。

## \[1.8.0] - 2026-11-25

### Added

- 新增检测时段控制（`monitor_schedule` 配置分组）：支持在某时段暂停检测（`blacklist` 模式，如夜间静音）或仅在某时段检测（`whitelist` 模式）
- 时段格式为 `HH:MM-HH:MM`，支持跨天（如 `22:00-07:00`）与多个时段，并可按星期限制生效日（`weekdays`，0=周一，留空表示每天）
- 暂停期间自动轮询与失败通知重试都会跳过；恢复检测后，暂停窗口内产生的提交会基于上次记录的 SHA 一次性补发，不会丢失
- 手动 `/github_monitor` 指令不受时段限制，`/github_status` 会显示当前时段控制状态
- 新增分支控制：仓库字典配置支持 `branches` 字段（分支列表），可一次监控多个分支；填 `["all"]` 或 `["*"]` 表示监控全部分支（每轮通过 GitHub API 枚举，自动覆盖新建分支）
- 新增 GitHub API 封装 `list_branches()`：分页获取仓库分支名列表，供全分支监控使用

### Changed

- 每个被监控分支独立使用 `owner/repo/branch` 作为数据与去重键，互不串扰；仓库分流规则按分支精确匹配的语义不变
- Issues 增量监控与分支无关，仍保持每仓库每轮只检查一次，推送目标沿用默认分支对应的分流规则
- `branch`（单分支）与 `branches` 同时配置时以 `branches` 为准；两者都未配置时维持原行为（监控默认分支）

### Compatibility

- 时段控制与分支控制均为新增配置项，默认关闭/不填，旧配置完全兼容

## \[1.7.1] - 2026-11-14

### Added

- 设置面板新增「仓库分流设置」独立配置分组（`repo_routing`）：通过模板列表逐条添加仓库分流规则，无需再手改 `repositories` 字典
- 每条规则支持 `owner` / `repo` / `branch` / `groups` / `notification_targets` / `exclusive`，并支持单条规则启用/停用
- 分流规则支持按分支精确匹配：指定 `branch` 的规则优先于不限定分支的规则；同一仓库存在多条同优先级规则时，后添加的生效
- 独立分流规则默认 `exclusive=true`，即只发给规则里的目标；需要与全局目标并集时将 `exclusive` 改为 `false`

### Changed

- 匹配到独立分流规则的仓库优先使用规则中的目标与 `exclusive` 设置；未匹配到时继续使用 `repositories` 内的仓库级配置，完全向后兼容
- `repositories` 中的 `groups` / `notification_targets` / `exclusive` 保留为兼容写法，并同步更新配置提示与 README
- `repo_routing.enabled` 可一键停用独立分流规则，回退到 `repositories` 仓库级配置

### Compatibility

- 独立分流面板使用 AstrBot `template_list` 配置类型，需要 AstrBot v4.10.4+；旧版本继续使用 `repositories` 内的仓库级字段，代码逻辑完全兼容
- 分流规则只负责推送目标，不会自动把仓库加入监控；监控仓库仍需配置在 `repositories` 中

## \[1.7.0] - 2026-11-14

### Added

- Commit 收集改为分页拉取，新增 `commit_fetch.max_commits`（默认 50，范围 1-500）：修复旧实现固定 `per_page=10` 且只取第一页、两次检查之间超过 10 个提交时旧提交被静默丢弃的问题；达到上限时通知中提示「更早提交未展示」
- 新增仓库级推送分流：仓库字典配置支持 `notification_targets`（仓库专属私聊，兼容别名 `private_targets`）和 `exclusive`（为 `true` 时不再叠加全局目标）；Commit、Issues 增量通知、合并转发、失败重试统一按仓库路由
- 新增 `commit_display.message_mode`：`full`（默认，完整展示并保留正文换行）/ `first_line`（只展示第一行/提交标题）；文本、图片卡片、合并转发节点同步生效
- 新增 Issues 增量通知：与 Commit 同轮检查，检测新增 Issue、新评论、标题/标签/评论数变化、Issue 关闭，并按「每仓库每轮一张卡」推送；文本卡附带标签、评论数、状态等进度信息，图片卡复用 Commit 卡片模板（`issue_output_format: text/image`）
- 图片卡片模板上下文新增 `count_text`、`summary_text`、`message_full`、`message_first_line`、`commits[].details`、`truncated`，自定义模板可用来区分 Commit / Issues 卡片

### Changed

- Commit 文本通知接入 `message_max_length` 分片逻辑，超长通知自动拆分为多条发送
- Issues open 列表改为分页获取（最多 500 条）；超过上限时跳过 Issue 关闭检测，避免把分页之外的 Issue 误判为已关闭
- Issues 评论正文按 `max_comment_length` 截断（此前配置项未实际生效）
- 统一主发送记录去重键为 `owner/repo/branch`，并纳入仓库专属私聊目标（私聊键带 `dm:` 前缀），修复重试时重复推送
- 合并转发失败重试保留 `truncated` 元数据

### Removed

- 移除 Issues cron 定时汇总（`issues_cron_enabled`、`issues_cron_expression`、`issues_push_min_interval`）及其定时任务、快照/推送日志逻辑；由随仓库轮询的 Issues 增量通知取代
- 移除不再使用的 `utils/cron_utils.py`

### Fixed

- 修复提交通知未按 `message_max_length` 拆分、长消息可能发送失败的问题
- 修复失败通知重试使用的仓库键缺少 branch，导致主发送记录去重失效的问题
- 修复合并转发成功时只标记群目标、未标记私聊目标的问题
- 修复 `max_comment_length` 配置项未生效的问题

## \[1.6.0] - 2026-10-27

### Added

- Commit 通知新增「变更统计」：在提交信息下方展示本次提交改动的文件数与增删行数（如 `📊 变更: +676 -34（8 个文件）`），文本通知与全部 6 套内置图片卡片模板同步支持
- 多个提交时额外展示「本轮总计」：汇总本轮所有提交的增删行数（如 `📊 本轮总计: +790 -40`），单提交场景不重复展示
- 新增 `commit_stats` 配置分组：`enabled`（默认开启，关闭后不产生额外 API 请求）、`max_commits`（默认 10，单次通知最多统计的提交数，填 0 表示不统计）
- 统计信息优先复用「获取最新提交」接口已返回的 `stats` / `files`；一次检测到多个提交时才对缺少统计的历史提交逐个补查，且只在确实要推送时查询，避免浪费 GitHub API 配额
- 合并提交、超大 diff 等 GitHub 不返回统计的场景自动跳过展示，不影响通知发送

### Changed

- 图片卡片模板上下文 `commits` 每项新增 `has_stats` / `stats_additions` / `stats_deletions` / `stats_files` 字段，自定义模板可直接使用

## \[1.5.0] - 2026-09-12

### Added

- 新增 Commit 通知「合并转发」推送模式（`forward_merge` 配置分组）：插件仍按原方式轮询配置的监听仓库，但本轮检测到的 commit 通知不再逐条发送，而是先逐条收集起来，随后按推送目标整合成一条（或按节点上限拆分的多条）合并转发聊天记录统一发送到对应的群 / 私聊，每个 commit 通知作为转发记录中的一个节点；仓库提交频繁时可有效避免刷屏
- 新增 `forward_merge.enabled`（默认关闭，关闭时维持原有的逐条发送行为）
- 新增 `forward_merge.content_mode`：`follow`（默认，跟随 `commit_output_format`，文字节点或图片卡片节点）/ `text`（一律文字节点）/ `image`（一律图片卡片节点，单节点渲染失败时自动回退为该节点的文字内容）
- 新增 `forward_merge.node_name` / `forward_merge.node_uin`：转发记录中每个节点展示用的昵称与账号（可填机器人自身 QQ 号）
- 新增 `forward_merge.max_nodes_per_message`（默认 20）：单条合并转发的最大节点数，超出后自动拆分为多条转发记录依次发送
- 新增 `forward_merge.fallback_to_normal`（默认开启）：qq_official / Telegram 等不支持合并转发的平台自动降级为逐条普通消息，保证不漏通知；关闭后这些目标会被判定为发送失败
- 同一批次内相同仓库的相同提交自动去重入队，避免上一轮整合发送中途异常时产生重复节点

### Changed

- commit 图片卡片渲染抽取为公共方法 `_render_commit_card_image`，普通推送与合并转发共用同一套渲染、异常捕获与失败回退逻辑
- 合并转发中发送失败的单个目标会写入待重试队列，下一轮轮询自动重试，其余目标不受影响

### Fixed

- 修复「合并转发」模式下通知可能永久丢失的问题：提交数据（`commits.json`）会在整合发送之前推进到最新 SHA，而待发送队列原先只存在于内存中，一旦进程崩溃、或整合发送过程中抛出未处理异常，该提交就会被记为已检查、队列里的通知却再也没有机会发出。现在队列在入队、发送前清空、写入重试队列时都会原子落盘（`pending_forward_notifications.json`，位于插件数据目录），插件启动时自动恢复上次未发送完的条目并在本轮重新入队补发
- 修复「合并转发」模式下发送失败的目标在下一轮 `retry_failed_notifications()` 中被当成普通提交通知逐条重试、把一条合并转发消息拆成多条普通消息的问题：失败载荷现在携带 `forward_merge` / `forward_batch` 元数据，重试时按推送目标重新合并为一个批次并走合并转发发送器，与首次发送保持一致（同一目标仍是一条转发记录，含 `max_nodes_per_message` 分片）
- commit 数据、已发送通知记录、Issues 快照 / 推送日志 / 监控状态、失败通知队列统一改为「写临时文件 + 原子替换」落盘，避免写入过程中进程退出留下截断的 JSON 导致整份记录丢失（新增 `utils/file_utils.py` 的 `write_json_atomic`）

## \[1.4.1] - 2026-09-05

### Added

- Issues 动态通知支持图片：新评论中的图片（GitHub 截图上传生成的 `<img src="...">` HTML 标签、markdown `![alt](url)` 语法、常见扩展名的裸图片直链）与通知文本合并为同一条消息发送（文字在前、图片紧随其后），正文中原位置替换为「[图片]」占位符；单条通知最多携带 6 张，图片下载/发送失败时自动降级为纯文本通知

## \[1.4.0] - 2026-08-26

### Added

- 新增项目仓库 Issues 动态监控模块：随每次仓库检查一同检测所配置监控仓库的 Issues，有新增 Issue、或 Issue 下出现新讨论（新增评论/标题正文标签更新）时立即推送通知，新评论附带评论者与内容摘要
- 新增 `issue_monitor_enabled` 配置项（默认开启），控制是否启用 Issues 动态监控；推送目标与 Commit 通知一致（私聊 + 全局群 + 仓库专属群）
- 首次纳入监控的仓库仅建立基线快照不推送，避免开启功能时被存量 Issues 刷屏

## \[1.3.6] - 2026-08-25

### Fixed

- 修复多平台同时运行时，直接填写官方 bot 的 openid（非数字会话ID）作为推送目标会被固定优先级自动检测误路由到 aiocqhttp 导致发送失败的问题：自动检测现按目标 ID 特征智能选平台——纯数字 ID → aiocqhttp 优先；非数字 ID（官方bot的十六进制 openid）→ qq_official / qq_official_webhook 优先，无需手动关闭其他适配器
- 修复 UMO 消息类型仅兼容枚举值（`GroupMessage`）的问题：现同时兼容枚举名（`GROUP_MESSAGE`），避免复制到枚举名时解析失败而静默回退自动检测

## \[1.3.5] - 2026-08-24

### Fixed

- 修复 QQ 官方机器人（qq_official / qq_official_webhook）无法作为主动推送目标的问题：移除推送链路中对群号/QQ号的纯数字硬校验（`int()` / `isdigit()`），十六进制 openid 等非数字会话ID不再被误判为非法
- 新增 UMO（unified_msg_origin）格式推送目标支持：`平台ID:消息类型:会话ID`，如 `aiocqhttp:GroupMessage:123456`、`小爱同学:GroupMessage:0771687B325FC423AD9F4C06A88D84E3`，适配多 bot / 多平台场景，可明确指定由哪个平台的哪个会话接收推送
- 向后兼容：纯数字群号/QQ号及 "-" 开头的 Telegram 群ID继续按原有逻辑处理；Issues 定时推送同步适配
- 图片通知的 OneBot 直发通道兼容 UMO 目标（平台为 aiocqhttp 且会话为数字ID时仍走直发）

## \[1.3.4] - 2026-08-12

### Added

- 新增 Commit 通知文转图功能，`commit_output_format` 配置项支持 `text` / `image` 两种输出格式
- 新增 6 套内置图片模板主题：terminal（终端风）、github_dark（GitHub 暗色）、light（简洁浅色）、retro_term（复古绿屏）、miku（初音风）、sakura（樱花粉），支持 `random` 随机切换；模板按内容裁剪无空白
- 新增自定义模板支持：在插件数据目录 `templates/` 下放置 `.html` 文件即可使用（Jinja2），并自动加入随机池
- 新增 `enable_base64_image` 配置项，图片传输可选 Base64 编码或本地文件路径；QQ 平台图片通知走 OneBot API 直发
- 新增群文件/群相册备份上传设置（`enable_group_file_upload`、`group_file_folder`、`enable_group_album_upload`、`group_album_name`、`group_album_strict_mode`）
- 新增 T2I 渲染参数配置（`t2i_image_type`、`t2i_quality`、`t2i_scale`），内置两轮渲染回退与图片完整性校验
- 图片通知渲染或发送失败时计入重试队列自动重试

## \[1.3.2] - 2026-07-02

### Added

- 新增 `/github_issues` 指令，查询当前用户所有仓库的 open issues
- 新增 Issues 定时推送功能，支持 Cron 表达式自动推送
- 新增 `issues_cron_enabled` 配置项，控制是否启用 Issues 定时推送
- 新增 `issues_cron_expression` 配置项，设置推送的 Cron 表达式
- 新增 `issues_push_min_interval` 配置项，设置相同内容推送的最小间隔
- 新增 issues 快照对比机制，只推送新增和更新的 issue
- 新增推送间隔保护，防止相同内容短时间内重复推送
- 新增群聊推送支持，Issues 变更通知可同时发送到私聊和群聊
- 优化消息发送逻辑

## \[1.2.5] - 2026-04-16

### Added

- GitHub 仓库 commit 监控功能
- 定时检查仓库更新并发送通知

## \[1.0.0] - 2026-03-20

### Added

- 初始版本发布
