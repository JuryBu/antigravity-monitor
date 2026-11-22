# Antigravity Monitor CLI 调用说明（单源契约）

本文档定义 `Antigravity Monitor` 面向自动化脚本、定时巡检及多代理规则的命令行调用契约。所有只读查询模式均**无需开启桌面界面、不联网请求上游大模型、不消耗模型额度、不修改本机 Antigravity 客户端状态**。

---

## 一、推荐入口与运行环境

推荐通过仓库根目录的无阻塞包装脚本 `agy-watch.cmd` 调用（支持 Windows 10/11，自动清空可能干扰 Python 3.9 的 `PYTHONUTF8`，按 `.venv -> venv -> Python 3.14..3.10 -> py launcher` 顺序选择解释器，**无 `pause` 阻塞**，并将 stdout/stderr 固定为 UTF-8 避免 Windows GBK 控制台输出字符异常）：

```powershell
# 单次结构化 JSON 体检（供自动化/规则解析首选，默认严格零写盘）
& '.\agy-watch.cmd' --json

# 附带指定对话（完整 UUID 或前缀）的上下文、请求体增量、缓存与保鲜状态
& '.\agy-watch.cmd' --json --conversation-id <conversation-uuid-or-prefix>

# 单次脱敏纯文本体检摘要（无参运行 agy-watch.cmd 默认等价于 --cli --readonly）
& '.\agy-watch.cmd' --cli
```

也可直接使用 Python 3.10+ 调用 `main.py`：

```powershell
python -B main.py --json --readonly --conversation-id <id>
```

> 说明：`运行单次体检.bat` 保留给桌面双击人工查看（末尾含 `pause`）；自动化与多对话并发调用请一律使用 `agy-watch.cmd` 或 `python main.py --json`。

---

## 二、命令行参数

| 参数 | 说明 |
|---|---|
| `--json` | 输出稳定结构化脱敏 JSON 体检快照后立即退出；默认启用严格只读（零写盘）。与 `--cli` 同时传入时以 `--json` 为准，保证 stdout 为纯合法 JSON |
| `--cli` | 输出脱敏纯文本体检摘要后立即退出；默认启用严格只读（零写盘） |
| `--readonly` | 显式声明严格零写盘（`--cli` 与 `--json` 已默认启用，不会写入或替换 `account_ledger.json` 与 `config.json`） |
| `--conversation-id <id>` | 定向附带指定对话（支持完整 UUID 或不区分大小写的前缀匹配）的实时上下文大小、最新请求体增量（`latest_context_delta`）、缓存命中、TTFT、保鲜倒计时及近 10 步明细，即使该对话不在最近 24 条活跃会话内也会按需定位读取 |
| `--account <label>` | 定向查看某单独历史账号（如 `al***1`、`be***2`，或 `__all__` 查看全机合并视图）的专属评级、对话、步数与日志；省略则自动跟随当前在线账号（刚换号尚无新步时自动沿用该号最近历史活跃数据定级） |
| `--range {30m,6h,today,7d}` | 指定观测时间范围，默认 `30m`（实时模式）；可选 `6h` / `today` / `7d` 历史范围 |
| `--clean-subagents` | 显式触发安全清理超过保护期的已结束老旧子代理以腾出本机 500 槽位（遵守 6 重防误删校验，绝不触碰主对话或未知 `.db`；默认移入系统回收站，按配置支持 `archive` / `delete`） |
| `--dry-run` | 配合 `--clean-subagents` 仅预览待清理条数与预计释放空间，严格零写盘 |
| `--min-age <minutes>` | 配合 `--clean-subagents` 指定保护期（分钟；硬下限锁死 `>= 30` 分钟，`0` 或省略沿用配置默认 `120` 分钟） |
| `--version` | 输出当前本地版本号与更新源（搭配 `--json` 返回结构化 JSON）后立即退出，严格零写盘 |
| `--check-update` | 联网查询 GitHub 远端 `version.json` 并报告是否有新版本与本地安装模式（`git` / `zip`，搭配 `--json` 返回结构化 JSON） |

---

## 三、语义化退出码

`--json` 与 `--cli` 模式返回语义化退出码（**不再恒返 0**），调用方可根据退出码结合 JSON 内 `pressure` 与 `grade` 综合判断：

| 退出码 | 对应 `pressure` | 含义与判定条件 |
|---|---|---|
| `0` | `normal` | 健康正常或在线新号待采样：`grade` 为 `T0` 或在线无异常 `WAIT`，且当前口径近 24 小时滚动有效步数（`effective_steps`）低于参考黄线（初始默认 `960`，可随账号历史沉淀 24h 峰值自适应调整） |
| `1` | `warn` / `red` / `degraded` | 需留意或存在异常：触及近 24 小时滚动步数参考黄线（`warn`）或参考红线（`red`，**仅作容量参考提醒，不等于账号实际降权；自动化策略应以持续性的 `grade`/`cache`/`perf`/`account` 实际状态为准，不凭瞬时波动或纯步数硬停**）、未恢复/未老化的异常掉缓存（含连续 `2` 步异常 `cr = 0` 高留意观察；主对话全绿时后台子代理偶发单步瞬抖自动降噪，不误拉黄主态）/疑似暗限速/偶发 1 次 503/重图上传超时/代理断流（`WARN`），或持续掉缓存（连续 `>= 3` 步异常或近 10 步高频异常）与服务端限流封禁（`T1` / `T2` / `T3`，`degraded`） |
| `2` | `offline_unknown` | 离线、证据不足或参数/采集失败：`grade == "IDLE"`（客户端未在线且无近 2 分钟实时轨迹）、离线指定无记录账号，或命令行参数错误、运行期读取异常（此时 `--json` 仍保证输出合法 JSON，含 `"ok": false` 与 `"error"`） |

---

## 四、`--json` 输出字段契约（`schema_version: "1.0"`）

顶层字段保证稳定存在（数值暂不可得时为 `null`，不伪造 `0`）：

| 字段路径 | 类型 | 说明 |
|---|---|---|
| `schema_version` | `string` | 契约版本号，当前固定 `"1.0"` |
| `ok` | `boolean` | 采集流程是否正常完成；异常退出 `2` 时为 `false` 并附 `error` |
| `observed_at` | `string` | UTC ISO-8601 采样时间戳（如 `2026-11-20T07:30:00Z`） |
| `observed_at_local` | `string` | 本机时区 ISO-8601 时间戳 |
| `observed_at_ts` | `number` | Unix 秒级时间戳 |
| `grade` | `string` | 详细健康等级：`IDLE \| WAIT \| T0 \| WARN \| T1 \| T2 \| T3`（`WAIT` 表示刚切入尚无历史与实时采样的冷账号待采样态） |
| `level` | `string` | 视觉灯号：`green \| cyan \| orange \| red` |
| `pressure` | `string` | 面向自动路由的 5 档综合压力：`normal \| warn \| red \| degraded \| offline_unknown`（由 `steps.effective_steps` 驱动：近 24h 步数破当前生效红线如初始 `>= 1200` 独立标为 `red`，不与一般 `warn` 混淆） |
| `exit_code` | `integer` | 与进程退出码一致：`0 \| 1 \| 2` |
| `headline` / `sub_headline` | `string` | 一句话状态结论与详细原因 |
| `active_model` | `string` | 主活跃对话的简称模型名（优先锁定主对话，不被短期子代理抢占） |
| `view` | `object` | 当前视角元信息：`view_account`、`resolved_account`、`available_accounts`、`is_custom_account_view`、`history_range`、`history_range_label`、`is_historical_rating`、`rating_anchor_time_local`、`range_steps` |

### 1. `account`（当前账号与分池配额）

| 字段 | 说明 |
|---|---|
| `short_label` / `email_masked` | 首尾掩码脱敏标签与邮箱（离线时 `short_label = "离线"`） |
| `online` | 本地 Language Server Hub 进程与 `GetUserStatus` 是否在线 |
| `tier` / `plan_status` | 账号订阅档位 ID（`userTier.id`）与套餐说明（来自 `planStatus.planInfo`） |
| `quota_family` | 当前主活跃模型归属池：`gemini` 或 `3p` |
| `five_hour_pct` / `weekly_pct` | 当前主用池 5h 剩余百分比（Hub 实时）与周剩余百分比（可选本机 Cockpit 只读补全） |
| `five_hour_reset_at` / `weekly_reset_at` | 当前主用池原生重置时间（ISO-8601 UTC 字符串，无则为 `null`） |
| `pools.gemini` / `pools.3p` | 分池详情对象，均含 `five_hour_pct`、`weekly_pct`、`five_hour_reset_at`、`weekly_reset_at`、`model_count`（完整遍历全部 `clientModelConfigs`，不截断） |

### 2. `steps`（近 24 小时滚动步数、自然日对照与主/子拆解）

| 字段 | 说明 |
|---|---|
| `scope_label` | 统计口径：在线时为当前账号脱敏短名，离线时为 `"全机"` |
| `window_mode` | 主步数统计滑窗模式：默认 `"rolling_24h"`（兼容未含 24h 字段的旧快照时回退 `"today"`） |
| `effective_steps` | 当前主保护线生效步数（`"rolling_24h"` 模式下等于 `rolling_24h_steps`；`remaining_to_warn` / `remaining_to_alert` / `usage_pct` 与顶层 `pressure` 均由本字段驱动） |
| `rolling_24h_steps` | 当前口径近 24 小时滚动累计步数（与 `daily_steps_warn` / `daily_steps_alert` 比较） |
| `account_24h_steps` / `total_24h_steps` | 当前账号近 24 小时步数 / 全机近 24 小时总步数 |
| `main_24h_steps` / `subagent_24h_steps` | 当前口径近 24 小时的**主对话步数**与**子代理步数**（服务端按整账号累计，`rolling_24h_steps = main_24h_steps + subagent_24h_steps`） |
| `total_main_24h_steps` / `total_subagent_24h_steps` | 全机口径近 24 小时的主对话步数与子代理步数 |
| `peak_hour_steps_24h` / `peak_hour_label_24h` | 当前口径近 24 小时单小时最高步数及对应本地整点时段（跨日标 `"昨日 HH:00-HH:00"`） |
| `today_steps` | 当前口径今日自然日累计步数（向后兼容保留，用于自然日对照） |
| `account_today_steps` / `total_today_steps` | 当前账号今日自然日步数 / 全机今日自然日总步数（向后兼容保留） |
| `main_today_steps` / `subagent_today_steps` | 当前口径今日自然日的**主对话步数**与**子代理步数**（`today_steps = main + subagent`，向后兼容保留） |
| `total_main_today_steps` / `total_subagent_today_steps` | 全机口径今日自然日的主对话步数与子代理步数（向后兼容保留） |
| `daily_steps_warn` / `daily_steps_alert` | 当前生效的黄线 / 红线阈值（初始建议默认 `960` / `1200`，基于紧凑小时桶计算的零重叠已沉淀 24h 历史峰值自动平滑上调或保守收紧） |
| `daily_steps_base_alert` / `daily_steps_limit_mode` / `daily_steps_limit_note` / `account_hist_safe_peak` | 可选自适应保护线元信息：配置初始基准红线、`default \| adaptive_up \| adaptive_down` 模式、人类可读调整依据与历史零重叠已沉淀 24h 纯健康峰值 |
| `remaining_to_warn` / `remaining_to_alert` | 基于 `effective_steps` 距黄线 / 红线的剩余步数（最低 `0`） |
| `usage_pct` | 基于 `effective_steps` 相对当前红线阈值的百分比 |
| `last_1h_steps` | 当前口径近 60 分钟步数（跨午夜连续滚动，不清零） |
| `peak_hour_steps_today` / `peak_hour_label` | 当前口径今日自然日单小时最高步数及对应时段（如 `"07:00-08:00"`，向后兼容保留） |

### 3. `cache`、`perf`、`errors_30m`、`errors_today`、`alerts`

- **`cache`**：`primary_hit_rate`、`primary_hit_classification`（`hit \| expected_cold \| abnormal_miss`）、`primary_hit_reason`（最新步缓存原因码，如 `cached` / `small_ctx` / `first_cacheable` / `account_switch` / `model_switch` / `compact_reset` / `revert_reset` / `rule_change` / `abnormal_zero`）、`consec_miss`（近窗最大连续异常归零步数）、`t1_consec_threshold`（固定 `3`，触发红灯 `T1 连续掉缓存` 所需连续异常归零步数；连续 `2` 步归橙色 `WARN` 高留意观察）、`max_abnormal_recent10`、`worst_recent10_hit_ratio`、`wasted_cold_tokens_total`、`min_freshness_sec`（仅追踪主对话，不被退休子代理抢占）、`min_freshness_conv`、`min_freshness_ctx`、`min_freshness_invalid_reason`、`freshness_window_sec`。
- **`perf`**：`tps`、`ttft_sec`、`ttft_avg_sec`、`ttft_max_sec`、`speed_status`（`ok \| slow \| unknown`；已排除极短输出 `<0.15s` 假 TPS 与高 TPS / 多模态原图上传耗时误判）。
- **`errors_30m` / `errors_today`**：按**当前在线账号**隔离统计的 `503`、`timeout`（动态提取真实时长 `5m`/`10m`/`90.5s` 的流超时，自动过滤 `<30s` 的非 LLM 本地毫秒超时，按 `ERROR_MESSAGE ±6s` 与 `USER_INPUT` 瞬时快照两阶段反查出事对话并自动分流重图大包上传超时 `heavy_media_timeout` 免误升 T2）、`403`（已过滤 `poll.go` 后台云同步权限不足误报，完整保留 `TOS_VIOLATION` 与真实推理 403）、`retry` 计数，以及 `errors_30m` 可选字段 `heavy_media_timeout`、`proxy_outage`、`proxy_outage_note`（跨接口 60s 爆发代理断流计数与说明，离线/历史视图不误升 `WARN`）、`window_min` 与 `t2_recovery_note`（`errors_today` 另附 `machine_*` 全机今日累计）；仅非重图服务端 `503 + timeout >= 2` 才升级红 `T2`，事后按主对话模型结合大上下文（`>=300K`）弹性校准与重图满速豁免评估恢复，严格保留真实 TTFT 展示且需 `>=2` 步常规步才回绿 `T0`。
- **`slots`**：本机 500 槽位只读盘点对象，含 `cap`（默认 `500`）、`total_db`、`remaining_slots`（兼出 `headroom`）、`usage_pct`、`total_bytes`、`main_count`（主对话数）、`protected_sub_count`（运行中/保护期内子代理）、`cleanable_sub_count`（超保护期可安全让位子代理）、`cleanable_bytes`、`cleanable_30m_count`（30m 硬下限极限可清数）、`unknown_count`、`pinned_floor_count`（不可清基底）、`protect_age_min`、`status`（`ok | warn | danger | main_at_risk`）、`main_at_risk`（是否已撞 30m 保护下限并危及主对话）、`risk_message`（兼出 `risk_headline`）、`summary_db_ok` 与 `last_auto_clean_summary`。
- **`models_recent`**：数组，按当前账号观测范围内各活跃模型独立汇总 `model`、`step_count`、`hit_count`、`abnormal_miss_count`、`ttft_avg_sec`、`ttft_max_sec`、`latest_ttft_sec`、`tps_avg`、`steps_after_last_t2`（最近一次 503/超时后的成功步数）与 `ttft_after_last_t2_median`（事后中位 TTFT，防多模态单步长尾误判）。
- **`alerts`**：数组，每项含 `{key, title, message}`；当 `pressure` 非正常或 `slots.main_at_risk == true` 时保证包含对应高危告警条目。
- **`--clean-subagents --json` 输出**：返回 `{ok, dry_run, mode, protect_age_min, candidate_count, removed_count, freed_bytes, skipped_locked_count, skipped_changed_count, remaining_db_count, archive_path, error}`（与 `--readonly` 冲突且未带 `--dry-run` 时返回 `ok: false` 与退出码 `2`）。

### 4. `counts`、`active_conversations`、`carryover_conversations` 与 `target_conversation`

- **`counts`**：`active_main`、`active_subagent`、`active_total`。
- **`active_conversations`**：最多 24 条最近活跃会话（**内置主对话槽位保护：最多保护 12 条活跃主对话，大批并发子代理不会把主对话挤出活跃观测或掩盖主对话 T1 掉缓存**），每条包含 `conversation_id`、`short_id`、`title`（对话标题，依次三源解析：`annotations/*.pbtxt` 本地元数据 → 在线本地 Hub `GetAllCascadeTrajectories` → 离线 `state.vscdb`）、`is_subagent`、`model`、`context_tokens`、`latest_context_delta`（相对上一步的请求体净变化）、`total_step_count`、`latest_hit_rate`、`latest_cache_reason`、`active_media_count`、`active_media_bytes`、`media_ttft_exempted`、`is_carryover_preview`、`large_ctx_ttft_allowance_sec`（`>=300K` 逼近压缩窗 TTFT 校准余量秒数，`<300K` 为 `0.0`）、`ttft_sec`、`ttft_avg_sec`、`ttft_max_sec`、`tps`、`consec_miss`、`abnormal_in_recent10`、`freshness_remaining_sec` 等。
- **`carryover_conversations`**：可选只读过渡列表（默认实时自动跟随视角下，最多 2 条近 10 分钟活跃但当前账号尚无新步的主对话入口，`is_carryover_preview: true`），严格与 `active_conversations` 及健康评级隔离，仅用于提示同窗口换号待首步及快速进入跨号长线详情。
- **`target_conversation`**：仅当传入 `--conversation-id` 时填充；除上述字段外，还包含 `recent_steps`（近 10 步逐拍 `total_tokens`、`context_delta`、`hit_rate`、`classification`、`reason`、`is_account_switch`、`active_media_count`、`active_media_bytes`）。同时顶层附 `target_conversation_query` 与 `target_conversation_found`。

---

## 五、只读边界与已知局限

1. **严格零写盘与并发安全**：`--cli`、`--json` 及 `--readonly` 均跳过 `account_ledger.json` 持久化写盘，多对话、多子代理并发调用不会触发 Windows `os.replace` `PermissionError` 竞态。
2. **上下文压缩重热识别**：系统通过 `transcript.jsonl` 中的 `CHECKPOINT` 事件结合上下文真实缩水（`< 0.85×`），或无事件时的上下文骤降（`< 0.75×`）识别压缩重热（`reason = "compact_reset"`, `classification = "expected_cold"`），既保证压缩耗时超过 180s 也不误判，也避免正文提及压缩关键词时产生假阳性。
3. **规则文件修改触发的前缀重置**：全局 `GEMINI.md` 规则文件修改触发的首步 `cr=0` 已自动识别为正常冷启（`reason = "rule_change"`, `classification = "expected_cold"`，短标 `规则冷`）；若是工作区级规则文件（如 `AGENTS.md`）修改触发的前缀重置，仍按单步轻量 `WARN`（`pressure = "warn"`）提示并在下一步恢复（连续 `2` 步归橙色 `WARN` 高留意观察，需连续 `>= 3` 步或近 10 步高频异常才会判定为 `T1 / degraded`）。
4. **周额度与重置时间来源**：5h 额度及原生 `resetTime` 直接来自本机在线 Antigravity Language Server Hub（最多扫描最近 40 个 `hub_*.json` 并先校验 PID 存活）；周额度与周重置时间在安装了第三方工具 Antigravity Cockpit 且**严格同邮箱匹配**时只读补全，未安装或离线时保持 `null`，绝不跨账号混用。
5. **全链路自动脱敏**：所有输出均对邮箱及账号标识执行首尾掩码处理，绝不读取、打印或持久化任何认证 Token。
