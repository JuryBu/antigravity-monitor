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

---

## 三、语义化退出码

`--json` 与 `--cli` 模式返回语义化退出码（**不再恒返 0**），调用方可根据退出码结合 JSON 内 `pressure` 与 `grade` 综合判断：

| 退出码 | 对应 `pressure` | 含义与判定条件 |
|---|---|---|
| `0` | `normal` | 健康正常或在线新号待采样：`grade` 为 `T0` 或在线无异常 `WAIT`，且当前口径今日步数低于黄线预警值（默认 `960`） |
| `1` | `warn` / `red` / `degraded` | 需降频或存在异常：触及今日步数黄线（`warn`，默认 `>= 960`）、触及今日步数红线（`red`，默认 `>= 1200`）、单步异常掉缓存/疑似暗限速/偶发 1 次 503（`WARN`），或连续/频繁掉缓存与限流封禁（`T1` / `T2` / `T3`，`degraded`） |
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
| `pressure` | `string` | 面向自动路由的 5 档综合压力：`normal \| warn \| red \| degraded \| offline_unknown`（步数破红线 `>= 1200` 独立标为 `red`，不与一般 `warn` 混淆） |
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

### 2. `steps`（今日步数与主/子拆解）

| 字段 | 说明 |
|---|---|
| `scope_label` | 统计口径：在线时为当前账号脱敏短名，离线时为 `"全机"` |
| `today_steps` | 当前口径今日累计步数（与 `daily_steps_warn` / `daily_steps_alert` 比较） |
| `account_today_steps` / `total_today_steps` | 当前账号今日步数 / 全机今日总步数 |
| `main_today_steps` / `subagent_today_steps` | 当前口径下的**主对话步数**与**子代理步数**（服务端按整账号累计，`today_steps = main + subagent`） |
| `total_main_today_steps` / `total_subagent_today_steps` | 全机口径的主对话步数与子代理步数 |
| `daily_steps_warn` / `daily_steps_alert` | 黄线 / 红线阈值（默认 `960` / `1200`） |
| `remaining_to_warn` / `remaining_to_alert` | 距黄线 / 红线的剩余步数（最低 `0`） |
| `usage_pct` | 相对红线阈值的百分比 |
| `last_1h_steps` | 当前口径近 60 分钟步数 |
| `peak_hour_steps_today` / `peak_hour_label` | 当前口径今日单小时最高步数及对应时段（如 `"07:00-08:00"`） |

### 3. `cache`、`perf`、`errors_30m`、`errors_today`、`alerts`

- **`cache`**：`primary_hit_rate`、`primary_hit_classification`（`hit \| expected_cold \| abnormal_miss`）、`consec_miss`（近窗最大连续异常归零步数）、`max_abnormal_recent10`、`worst_recent10_hit_ratio`、`wasted_cold_tokens_total`、`min_freshness_sec`（仅追踪主对话，不被退休子代理抢占）、`min_freshness_conv`、`min_freshness_ctx`、`min_freshness_invalid_reason`、`freshness_window_sec`。
- **`perf`**：`tps`、`ttft_sec`、`ttft_avg_sec`、`ttft_max_sec`、`speed_status`（`ok \| slow \| unknown`）。
- **`errors_30m` / `errors_today`**：按**当前在线账号**隔离统计的 `503`、`timeout`（5m 流超时）、`403`、`retry` 计数（附 `scope_label`；`errors_30m` 附 `window_min` 与 `t2_recovery_note` 事后恢复说明；`errors_today` 另附 `machine_*` 全机今日累计）；切换账号时前序账号的 503/超时不会连坐新号，同号内 503 后若当前模型连续稳定恢复响应且 TTFT 正常则自动从 `T2` 降级观察直至回绿。
- **`models_recent`**：数组，按当前账号近 30 分钟内各活跃模型独立汇总 `model`、`step_count`、`hit_count`、`abnormal_miss_count`、`ttft_avg_sec`、`ttft_max_sec`、`latest_ttft_sec`、`tps_avg` 与 `steps_after_last_t2`（最近一次 503/超时后的成功步数）。
- **`alerts`**：数组，每项含 `{key, title, message}`；当 `pressure` 非正常时至少包含当前状态诊断条目。

### 4. `counts`、`active_conversations` 与 `target_conversation`

- **`counts`**：`active_main`、`active_subagent`、`active_total`。
- **`active_conversations`**：最多 24 条最近活跃会话（**内置主对话槽位保护：最多保护 12 条活跃主对话，大批并发子代理不会把主对话挤出活跃观测或掩盖主对话 T1 掉缓存**），每条包含 `conversation_id`、`short_id`、`title`（对话标题，依次三源解析：`annotations/*.pbtxt` 本地元数据 → 在线本地 Hub `GetAllCascadeTrajectories` → 离线 `state.vscdb`）、`is_subagent`、`model`、`context_tokens`、`latest_context_delta`（相对上一步的请求体净变化）、`total_step_count`、`latest_hit_rate`、`ttft_sec`、`ttft_avg_sec`、`ttft_max_sec`、`tps`、`consec_miss`、`abnormal_in_recent10`、`freshness_remaining_sec` 等。
- **`target_conversation`**：仅当传入 `--conversation-id` 时填充；除上述字段外，还包含 `recent_steps`（近 10 步逐拍 `total_tokens`、`context_delta`、`hit_rate`、`classification`、`reason`）。同时顶层附 `target_conversation_query` 与 `target_conversation_found`。

---

## 五、只读边界与已知局限

1. **严格零写盘与并发安全**：`--cli`、`--json` 及 `--readonly` 均跳过 `account_ledger.json` 持久化写盘，多对话、多子代理并发调用不会触发 Windows `os.replace` `PermissionError` 竞态。
2. **上下文压缩重热识别**：系统通过 `transcript.jsonl` 中的 `CHECKPOINT` 事件结合上下文真实缩水（`< 0.85×`），或无事件时的上下文骤降（`< 0.75×`）识别压缩重热（`reason = "compact_reset"`, `classification = "expected_cold"`），既保证压缩耗时超过 180s 也不误判，也避免正文提及压缩关键词时产生假阳性。
3. **规则文件修改触发的前缀重置**：若对话中途修改了注入到系统提示词中的规则文件，下一拍可能出现单步 `cr=0` 重热（随后立即恢复 98%+ 命中）；单步归零仅触发轻量 `WARN`（`pressure = "warn"`），需连续 `>= 2` 步或近 10 步高频异常才会判定为 `T1 / degraded`。
4. **周额度与重置时间来源**：5h 额度及原生 `resetTime` 直接来自本机在线 Antigravity Language Server Hub（最多扫描最近 40 个 `hub_*.json` 并先校验 PID 存活）；周额度与周重置时间在安装了第三方工具 Antigravity Cockpit 且**严格同邮箱匹配**时只读补全，未安装或离线时保持 `null`，绝不跨账号混用。
5. **全链路自动脱敏**：所有输出均对邮箱及账号标识执行首尾掩码处理，绝不读取、打印或持久化任何认证 Token。
