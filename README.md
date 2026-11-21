# Antigravity Monitor（反重力监控）

面向 Windows 10 / 11 的轻量本地只读桌面监控与命令行遥测工具，用于实时观测 Antigravity 2.x 的**服务端缓存命中率、请求体大小变化轨迹、真实响应速度（TTFT 与 tok/s）、大上下文保鲜倒计时、今日请求步数保护线、分池配额与重置时间，以及底层 `language_server.log` 滑动窗口异常**。

## 核心特点

- **纯只读、零侵入、并发安全**：不修改 Antigravity 快捷方式或第三方切换工具启动参数，不开 CDP 调试端口，不拦截网络流量，不上传任何账号凭据；命令行模式默认严格零写盘，多对话并发调用无文件锁竞态。
- **四色防误报缓存引擎**：逐轮解析 `transcript.jsonl`，精准区分四类状态并分别着色：
  - **绿（缓存命中）**：正常热缓存命中；
  - **紫（压缩重热）**：结合 `CHECKPOINT` 事件与上下文体量真实缩水判定，压缩耗时长也不误报，正文提及压缩关键词也不会触发假阳性；
  - **橙（正常冷启）**：首轮、闲置超时自然重热、切号首拍、切模型、小上下文及首次跨过 32K 门槛；
  - **红（异常掉缓存）**：同模型短间隔内异常 `cr = 0`，防单步偶中「洗白」且支持按切号账本隔离历史账号红条。
- **主对话 / 子代理分类与多主对话并行**：自动识别主对话与子代理（`主对话 / 子代理 / 全部` 切换，内置主对话保护槽位防止大批子代理挤爆列表）；支持 24 条活跃对话滚动与底边自由拉高（`3～12` 行）；支持右键或详情页 **📌 聚焦指定主对话**到主面板，大上下文保鲜卡悬停可一览全部活跃大上下文主对话倒计时。
- **单对话长线体检弹窗（实时动态更新 + 模型切换色带）**：左键点击任意对话行即可打开无边框长线详情窗口，随主面板 2.5s 轮询**实时动态刷新**新步与保鲜倒计时（随主窗收起或隐藏自动同步关闭）；支持 `近 60 步 / 全部步数` 切换、三图联动鼠标十字准星、**顶部彩色模型时间色带与切换虚线**（每步精准标注模型）、请求体增长与紫色压缩曲线、TTFT 45s/90s 参考线（含异常长尾柔封顶）及四色缓存长线，跨天自动显示 `MM-DD` 日期。
- **按账号隔离的 30 分钟日志滑动窗、流超时反查与抗横跳恢复判定**：按字节偏移增量读取 `language_server.log`（剥离 glog 微秒前缀并过滤 `telemetry.go` / `recordTrajectoryAnalytics` 等后台遥测上报，对同 goroutine / `errorreport.go` 紧邻复读行去重合并，精准区分 `503 No capacity` 与 `503 Unavailable`），严格按切号账本隔离当前账号的 `503 / 流超时 / 403`（切号瞬间清零，绝不连坐新号）；动态提取流超时真实时长（如 `10m` / `5m`）并按 `±6s` 反查出事对话短 ID 与标题；单次偶发流超时或单次 503 报橙色 `WARN`，累计 `>= 2` 次才升级红色 `T2`；事后按主对话模型中位 TTFT 评估恢复（排除子代理与轻量免疫模型误洗白，容忍单步多模态长尾防 `WARN <-> T2` 横跳）。
- **本机 500 会话槽位水位监控与安全子代理让位清理**：实时只读盘点本机 `conversations/*.db`（忽略 `.db-shm` 误触 mtime），点击主面板槽位条可展开「本机 500 槽位分布」详情窗查看主对话、可清子代理、保护中子代理与未知文件分布；支持一键手动清理 2 小时外已结束子代理，并默认开启自动让位清理（在 Antigravity 关闭空闲或高水位时自动执行，随时可关，默认移入 Windows 系统回收站可还原，6 重防误删护栏 + 30 分钟硬下限绝不突破；撞下限危及主对话时主动弹出红警提醒）。
- **全用户自动更新提醒与可开启自动更新重启**：启动 8 秒后在后台低频检查 GitHub 远端 `version.json`（默认每 30 分钟，零阻塞热路径）；发现新版本时自动在展开面板浮出青蓝更新条、收起顶栏亮 `↑vX.Y` 角标并推一条系统托盘通知；同时支持 **Git 克隆用户（严格干净工作区 `--ff-only` 快进）** 与 **直接下载 ZIP 压缩包/未装 Git 用户（官方 ZIP 校验 + 自动备份回滚 + 原子替换，绝不覆盖 `config.json` 与 `account_ledger.json`）**；可在更新条、右键菜单或托盘菜单随时开启 **「发现更新时自动更新重启」**，后续发版自动拉取并无缝重启生效。
- **跨账号 TTFT 连续衔接与完整单对话详情窗**：当同一对话先后跨越多个账号时，主面板通过缓存化的全局对齐投影自动找回该账号更早在此对话的真实 TTFT 柱子；右侧单对话长线详情窗永远展示该对话自身完整轨迹，不被左侧账号筛选切碎。
- **面向自动化的无阻塞 CLI & `--json` 契约**：提供无 `pause` 包装入口 `agy-watch.cmd`，支持 `--json`、`--cli`、`--readonly`、`--conversation-id <id>`、`--account`、`--range` 及显式 `--clean-subagents [--dry-run] [--min-age]`，详见 [CLI 调用说明](docs/CLI调用说明.md)。

## 健康评级与压力说明

| 评级 | 灯号 | 综合压力 (`pressure`) | CLI 退出码 | 判定条件 |
|---|---|---|---|---|
| **IDLE 离线** | 青/灰 | `offline_unknown` | `2` | 客户端未运行且无近 2 分钟实时轨迹，暂停实时异常告警 |
| **T0 健康** | 绿灯 | `normal`（超步数线时为 `warn`/`red`） | `0`（超步数线为 `1`） | 近 30 分钟缓存、TTFT 与底层日志均健康（今日步数超黄/红线仅作独立保护提醒，不覆盖真实 T0/T1/T2 评级） |
| **留意预警** | 橙灯 | `warn` | `1` | 单步异常归零、偶发 1 次 503 或 1 次流超时，或疑似暗限速 |
| **T1 掉缓存** | 红灯 | `degraded` | `1` | 活跃会话连续 `>= 2` 步异常 `cr = 0`，或近 10 步异常 `>= 4` 次且命中率 `< 50%` |
| **T2 限流黑洞** | 红灯 | `degraded` | `1` | 近 30 分钟累计 `503 + 流超时 >= 2` 次且主对话模型事后未稳定恢复 |
| **T3 封禁告警** | 红灯 | `degraded` | `1` | 近 30 分钟检测到 `403 TOS_VIOLATION / PERMISSION_DENIED` |

## 数据源说明（全部本地只读）

| 数据源 | 本地路径 | 用途 |
|---|---|---|
| 对话轨迹日志 | `%USERPROFILE%\.gemini\antigravity\brain\<id>\.system_generated\logs\transcript.jsonl` | 逐轮 `input_tokens`、`cache_read_tokens`、请求体增量 `±ΔK`、`CHECKPOINT` 压缩与 `Undo` 回溯事件、`ERROR_MESSAGE` 报错反查 |
| 对话元数据库 | `%USERPROFILE%\.gemini\antigravity\conversations\<id>.db`（`mode=ro`） | `TTFT`、`streaming`、`tok/s`、`maxContextTokens` 与单对话全量长线速度曲线 |
| 会话摘要索引库 | `%USERPROFILE%\.gemini\antigravity\conversation_summaries.db`（`mode=ro`） | 500 槽位主对话 / 子代理分类识别与 6 重防误删校验 |
| 语言服务日志 | `%APPDATA%\Antigravity\logs\language_server.log` | 增量捕获 `503 No capacity`、动态时长流超时、`403 封禁` 与代理断连重试 |
| 本地在线 Hub | `%USERPROFILE%\.gemini\antigravity\daemon\hub_*.json`（`127.0.0.1`） | 只读 `GetUserStatus` 获取脱敏账号、套餐档位及 `gemini` / `3p` 分池 5h 额度与原生 `resetTime` |
| Cockpit 历史库（可选） | `%USERPROFILE%\.antigravity_cockpit\platform-quota-history-v1.sqlite` | 严格同邮箱只读补全分池周额度与 `reset_at`；未安装或离线时自动跳过 |

## 快速开始

### 1. 安装依赖（Windows 10 / 11，推荐 Python 3.10–3.14）

```powershell
pip install -r requirements.txt
```

### 2. 桌面监控启动与日常交互

- **无黑框启动**：双击 `启动反重力监控.vbs`（优先自动检测 `.venv` / `venv` 及本机 Python 3.10–3.14）
- **交互手势**：
  - 拖拽顶部胶囊移动位置并自动记忆坐标；轻点顶部胶囊展开/收起详情；
  - 鼠标悬停活跃对话行查看近 10 步逐拍请求体大小、净变化 `±ΔK`、TTFT 与缓存原因；
  - **左键点击任意活跃对话行**打开「单对话长线详情窗口」查看完整增长/TTFT/缓存三曲线；
  - **右键任意活跃对话行**快速查看详情或 **📌 聚焦该对话**到主面板吞吐与保鲜卡；
  - 拖拽窗口底部细灰条自由调节列表显示行数（`3～12` 行），滚动条与滚轮浏览最多 24 条对话；
  - 点击右上角 `主题:星蓝 ⇄` 实时切换「午夜星蓝 / 深炭极简」双主题。

### 3. 命令行与多代理自动化调用

详细 JSON 字段契约、退出码与并发只读边界见 [docs/CLI调用说明.md](docs/CLI调用说明.md)：

```powershell
# 无阻塞 JSON 结构化体检（默认严格零写盘，返回语义退出码 0/1/2）
.\agy-watch.cmd --json

# 定向附带指定对话详情
.\agy-watch.cmd --json --conversation-id <conversation-id-or-prefix>

# 脱敏文字摘要
.\agy-watch.cmd --cli
```

### 4. 桌面快捷方式、开机自启与测试

```powershell
# 创建桌面快捷方式并启用 Startup 文件夹开机自启（零写注册表）
python scripts/install_shortcuts.py --enable-startup

# 卸载桌面快捷方式并取消开机自启
python scripts/install_shortcuts.py --disable-startup --remove-desktop

# 运行全部单元测试
python -m unittest discover -s tests -v
```

## 隐私边界与许可

- 所有剪贴板摘要、系统通知、GUI 详情报告及 CLI / JSON 输出均对邮箱与账号标识执行自动首尾掩码脱敏，绝不记录或传输任何 Token。
- 本项目采用 [MIT License](https://opensource.org/licenses/MIT) 开源许可。
