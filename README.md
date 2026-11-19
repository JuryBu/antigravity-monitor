# Antigravity Monitor（反重力监控）

面向 Windows 10 / 11 的轻量本地只读桌面监控工具，用于实时观测 Antigravity 2.x 的**服务端缓存命中率、真实响应速度（TTFT 与 tok/s）、大上下文保鲜倒计时、今日请求步数保护线、当前账号脱敏配额，以及底层 `language_server.log` 滑动窗口异常**。

## 核心特点

- **纯只读、零侵入**：不修改 Antigravity 快捷方式或第三方切换工具启动参数，不开 CDP 调试端口，不拦截网络流量，不上传任何账号凭据；客户端重启、切号或卡死时依然独立常驻。
- **防误报缓存引擎**：逐轮解析 `transcript.jsonl`，自动识别并排除「新对话首轮」「闲置超时自然重热」「中途切模型」「`/compact` 上下文裁剪」「低于 32K 隐式缓存门槛」及「首次跨过 32K 门槛」等正常归零，精准揪出连续异常 `cr=0`，并估算近 10 步多耗冷算 Token。
- **SQLite 只读速度诊断**：采用 `(db + wal)` 双文件戳增量缓存与有界 LRU（上限 12 槽位），纯只读解析 `conversations/*.db` 的 `gen_metadata`，实时显示首字延迟（`TTFT`）与流式生成速度（`tok/s`），第一时间发现「命中缓存但生成掉速」的暗限速状态。
- **30 分钟滑动窗口日志监控**：按字节偏移增量读取 `%APPDATA%\Antigravity\logs\language_server.log`（兼容客户端重启截断归零与半行防劈裂），同时展示「近 30 分钟实时窗口 / 今日累计」，历史偶发 `503` 滑出窗口后自动衰减恢复绿灯。
- **原生桌面悬浮窗与系统托盘**：Win11 DWM 原生圆角无边框胶囊，支持左键拖拽记忆多屏坐标、轻点展开/收起详情、面板与托盘右键原生勾选菜单；内置「午夜星蓝（默认）」与「深炭极简」双主题实时切换；支持单实例防双开二次双击唤出，以及零写注册表的 `Startup` 开机自启一键开关。

## 健康评级说明

| 评级 | 灯号 | 判定条件 |
|---|---|---|
| **T0 健康** | 绿灯 | 近 30 分钟缓存与底层日志均正常，无连续掉缓存与限流异常 |
| **留意预警** | 橙灯 | 出现单步异常归零、偶发 1 次 503、疑似暗限速（低 `tok/s` 或异常高 `TTFT`），或今日步数触及预警线（默认 800） |
| **T1 掉缓存** | 红灯 | 活跃对话在同模型短间隔内连续 `>= 2` 步异常 `cr = 0`（此时大上下文保鲜同步标为失效） |
| **T2 限流黑洞** | 红灯 | 近 30 分钟出现 5 分钟生成流超时或 `>= 2` 次 `503 No capacity` |
| **T3 封禁告警** | 红灯 | 近 30 分钟检测到 `403 TOS_VIOLATION / PERMISSION_DENIED` |

## 数据源说明（全部本地只读）

| 数据源 | 本地路径 | 用途 |
|---|---|---|
| 对话轨迹日志 | `%USERPROFILE%\.gemini\antigravity\brain\<id>\.system_generated\logs\transcript.jsonl` | 逐轮 `input_tokens`、`cache_read_tokens`、模型、今日步数与 10 步微柱图 |
| 对话元数据库 | `%USERPROFILE%\.gemini\antigravity\conversations\<id>.db`（`mode=ro`） | `TTFT`、`streaming`、`tok/s`、`maxContextTokens` 与暗限速诊断 |
| 语言服务日志 | `%APPDATA%\Antigravity\logs\language_server.log` | 增量捕获 `503 No capacity`、`5m 流超时`、`403 封禁` 与代理断连重试 |
| 本地在线 Hub | `%USERPROFILE%\.gemini\antigravity\daemon\hub_*.json`（`127.0.0.1`） | 只读 `GetUserStatus` 获取脱敏账号掩码与当前模型池 5h 额度 |
| Cockpit 历史库（可选） | `%USERPROFILE%\.antigravity_cockpit\platform-quota-history-v1.sqlite` | 若本机安装第三方工具 Antigravity Cockpit，则按同邮箱只读补全周额度；未安装时自动跳过不影响运行 |

## 快速开始

### 1. 安装依赖（Windows 10 / 11，推荐 Python 3.10–3.13）

```powershell
pip install -r requirements.txt
```

### 2. 启动与日常交互

- **无黑框启动**：双击 `启动反重力监控.vbs`（优先自动检测目录内 `.venv` / `venv` 虚拟环境）
- **命令行启动**：`python main.py`（可选参数：`--minimized` 启动时缩进托盘，`--cli` 单次输出脱敏文字体检摘要）
- **界面操作**：左键按住顶部胶囊拖拽至任意屏幕角落并自动记忆坐标；轻点顶部胶囊（位移 `< 4px`）展开/收起详情；点击右上角 `主题:星蓝 ⇄` 实时切换双主题；点击 `—` 缩进右下角系统托盘；左键托盘图标显示/隐藏，右键托盘或面板任意区域呼出完整勾选菜单。

### 3. 桌面快捷方式与开机自启（可随时卸载）

```powershell
# 创建桌面快捷方式并启用 Startup 文件夹开机自启（零写注册表）
python scripts/install_shortcuts.py --enable-startup

# 卸载桌面快捷方式并取消开机自启
python scripts/install_shortcuts.py --disable-startup --remove-desktop
```

### 4. 自定义配置与自动化测试

- 复制 `config.example.json` 为 `config.json`（已纳入 `.gitignore`）可自定义刷新频率、保鲜窗口秒数、步数保护线、滑动窗分钟数与透明度。
- 运行全部单元测试：

```powershell
python -m unittest discover -s tests -v
```

## 隐私边界与许可

- 所有剪贴板摘要、系统通知与 CLI 输出均对邮箱与用户名执行自动首尾掩码脱敏，绝不记录或传输任何 Token。
- 本项目采用 [MIT License](https://opensource.org/licenses/MIT) 开源许可。
