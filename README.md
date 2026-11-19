# Antigravity Monitor（反重力监控）

面向 Windows 10 / 11 的轻量本地只读桌面监控与命令行遥测工具，用于实时观测 Antigravity 2.x 的**服务端缓存命中率、请求体大小变化轨迹、真实响应速度（TTFT 与 tok/s）、大上下文保鲜倒计时、今日请求步数保护线、分池配额与重置时间，以及底层 `language_server.log` 滑动窗口异常**。

## 核心特点

- **纯只读、零侵入、并发安全**：不修改 Antigravity 快捷方式或第三方切换工具启动参数，不开 CDP 调试端口，不拦截网络流量，不上传任何账号凭据；命令行模式默认严格零写盘，多对话并发调用无文件锁竞态。
- **四色防误报缓存引擎**：逐轮解析 `transcript.jsonl`，精准区分四类状态并分别着色：
  - **绿（缓存命中）**：正常热缓存命中；
  - **紫（压缩重热）**：识别 `CHECKPOINT` / `<CONTEXT_SUMMARY>` 与上下文骤降，压缩耗时长也不误报；
  - **橙（正常冷启）**：首轮、闲置超时自然重热、切号首拍、切模型、小上下文及首次跨过 32K 门槛；
  - **红（异常掉缓存）**：同模型短间隔内异常 `cr = 0`，防单步偶中「洗白」且支持按切号账本隔离历史账号红条。
- **主对话 / 子代理分类与多主对话并行**：自动识别主对话与子代理（`主对话 / 子代理 / 全部` 切换，内置主对话保护槽位防止大批子代理挤爆列表）；支持 24 条活跃对话滚动与底边自由拉高（`3～12` 行）；支持右键或详情页 **📌 聚焦指定主对话**到主面板，大上下文保鲜卡悬停可一览全部活跃大上下文主对话倒计时。
- **单对话长线体检弹窗（3 张交互图表）**：左键点击任意对话行即可打开无边框长线详情窗口，支持 `近 60 步 / 全部步数` 切换与三图联动十字准星，直观查看**请求体大小增长与压缩曲线、TTFT 长线趋势（含 45s/90s 参考线）、四色缓存命中长线轨迹**及每步 `±ΔK` 增量。
- **SQLite 只读速度诊断与 30 分钟日志滑动窗**：采用 `(db + wal)` 双文件戳增量缓存只读解析 `conversations/*.db`；按字节偏移增量读取 `language_server.log`（剥离 glog 微秒前缀，防误匹配 `503`），支持客户端离线静默待机（`IDLE`）。
- **面向自动化的无阻塞 CLI & `--json` 契约**：提供无 `pause` 包装入口 `agy-watch.cmd`，支持 `--json`、`--cli`、`--readonly`、`--conversation-id <id>`、语义化退出码（`0/1/2`）与分池配额/重置时间透传，详见 [CLI 调用说明](docs/CLI调用说明.md)。

## 健康评级与压力说明

| 评级 | 灯号 | 综合压力 (`pressure`) | CLI 退出码 | 判定条件 |
|---|---|---|---|---|
| **IDLE 离线** | 青/灰 | `offline_unknown` | `2` | 客户端未运行且无近 2 分钟实时轨迹，暂停实时异常告警 |
| **T0 健康** | 绿灯 | `normal` | `0` | 近 30 分钟缓存与底层日志均正常，且今日步数 `< 960` |
| **留意预警** | 橙灯 | `warn` | `1` | 单步异常归零、偶发 1 次 503、疑似暗限速，或今日步数达到黄线（默认 `960`） |
| **步数红线** | 红灯 | `red` | `1` | 当前账号今日累计步数达到保护红线（默认 `1200`） |
| **T1 掉缓存** | 红灯 | `degraded` | `1` | 活跃会话连续 `>= 2` 步异常 `cr = 0`，或近 10 步异常 `>= 4` 次且命中率 `< 50%` |
| **T2 限流黑洞** | 红灯 | `degraded` | `1` | 近 30 分钟出现 5 分钟流超时或 `>= 2` 次 `503 No capacity` |
| **T3 封禁告警** | 红灯 | `degraded` | `1` | 近 30 分钟检测到 `403 TOS_VIOLATION / PERMISSION_DENIED` |

## 数据源说明（全部本地只读）

| 数据源 | 本地路径 | 用途 |
|---|---|---|
| 对话轨迹日志 | `%USERPROFILE%\.gemini\antigravity\brain\<id>\.system_generated\logs\transcript.jsonl` | 逐轮 `input_tokens`、`cache_read_tokens`、请求体增量 `±ΔK`、`CHECKPOINT` 压缩事件、主/子步数统计 |
| 对话元数据库 | `%USERPROFILE%\.gemini\antigravity\conversations\<id>.db`（`mode=ro`） | `TTFT`、`streaming`、`tok/s`、`maxContextTokens` 与单对话全量长线速度曲线 |
| 语言服务日志 | `%APPDATA%\Antigravity\logs\language_server.log` | 增量捕获 `503 No capacity`、`5m 流超时`、`403 封禁` 与代理断连重试 |
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
