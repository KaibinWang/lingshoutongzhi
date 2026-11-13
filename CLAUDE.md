# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

零售通知自动分发工具 (Retail Notification Auto-Distribution Tool) — a Windows desktop PyQt5 app that distributes notification messages to WeCom (企业微信 / WeChat Work) targets. It has two independent delivery mechanisms in one tabbed UI:

1. **Group sending** (群组发送): posts Markdown to WeCom robot **webhook** URLs over HTTP.
2. **Personal sending** (个人发送): automates the WeCom PC client via `pyautogui` (search contact → OCR-verify department → click → paste message → Enter), and can attach files via the Windows clipboard `CF_HDROP` format.

All UI text, comments, and log messages are in Chinese. Target platform is Windows only (relies on `pywin32`, `pyautogui`, PaddleOCR).

## Critical Environment Constraints

- **Python 3.10 is required.** `paddlepaddle==2.6.2` (pinned in `requirements.txt`) is incompatible with other versions. The included `venv/` is Python 3.10.11.
- **The project path must not contain non-ASCII (Chinese) characters**, or the Qt app fails to start (per `README.txt`). The current path is fine.
- The app automates the user's mouse/keyboard during personal sending — it is inherently interactive and cannot be fully tested headless. WSL2/Linux cannot run the GUI or PaddleOCR flows; development happens on Windows.

## Commands

```bash
# Activate the bundled virtualenv (Windows)
venv\Scripts\activate.bat

# Install dependencies (mirror used by build.bat)
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn

# Run the app
python main.py

# Package with PyInstaller (produces dist\RetailNotifier\零售通知工具.exe)
# 输出目录必须是 ASCII：Paddle 的 C++ 推理引擎在 Windows 上打不开中文路径下的模型文件
# （见 ocr_helper 的 _local_model_dir / _ensure_ascii_copy）。
build.bat
# or: pyinstaller "零售通知工具.spec"
```

## Architecture

### Entry point
`main.py` sets `BASE_DIR` (handles frozen/PyInstaller vs. dev layout), 做两件打包加固，然后配置 logging 到 `logs/app.log`，再启动 PyQt5 `QApplication` 与 `MainWindow`：
- `_ensure_std_streams()` —— spec 里 `console=False` → exe 是 **WINDOWS_GUI 子系统**，**双击**启动时没有控制台可继承，Python 会把 `sys.stdout/stderr` 置为 `None`，于是 paddle/paddleocr 里那些**裸写** `sys.stdout.write(...)` 的分支（`paddle/base/core.py` 的 AVX 提示、`ppocr/utils/utility.py::check_install`）会 `AttributeError` 直接崩 —— 尤其是**在别人机器上**（本机 CPU 支持 AVX，走不到那个分支，所以"我这儿好好的"）。这里在导入任何第三方库之前把它们换成 `os.devnull`（指文件会让 OCR 的逐块 `print` 把日志撑爆，且内容与 `app.log` 重复）。用 `.bat` 启动只是**绕过**（继承控制台句柄），不是修好，且用户仍会去双击 exe —— 所以修在代码里。
- `_install_crash_logger()` —— 打包后 Qt 槽里逃出的未捕获异常会让进程直接 abort，**屏幕上什么都没有**（无控制台时连 stderr 都没有）、缓冲区也不 flush。`sys.excepthook` 写 `logs/crash.log`（flush + `os.fsync`）是现场唯一取证手段。

### `core/` — business logic (no UI)
- `config.py` — three JSON-backed config managers, each persisting to `data/`:
  - `GroupConfig` → `groups.json`: `[{id, name, webhook, category, enabled}]`. Also CSV/Excel import/export.
  - `TemplateConfig` → `templates.json`: `[{id, name, content}]` Markdown message templates containing `{placeholder}` variables (e.g. `{topic}`, `{time}`). Seeds 3 defaults if the file is missing.
  - `PersonConfig` → `persons.json`: `[{id, name, unit, tag, enabled}]`. `tag` is a comma-separated list used for filtering. CSV/Excel import/export, `clear_all()`.
  - All follow the same pattern: `load()` on init (empty list on parse error), `save()` writes UTF-8 JSON with `ensure_ascii=False`.
- `sender.py` — `WebhookSender` (HTTP POST of Markdown to `qyapi.weixin.qq.com/cgi-bin/webhook/send` with `interval`/`retries`/`timeout`; honors a `_stop_flag`), `SendResult`, `SendLog` (per-run log + `logs/history.json`), plus the shared failure-localization vocabulary: `STAGE_LABELS` (中文阶段标签) and `format_results(task)`（把落盘的逐条结果渲染成人类可读行，审批工作台与经办人面板共用）。`SendResult` 除 `group_name/success/message/timestamp/unit/content` 外还有**可选尾部字段** `stage/detail/attempts/screenshot/elapsed`（个人发送用；群组发送保持默认值，故 `SendResult("n", True, "m")` 三参调用仍可用）。
- `personal_sender.py` — `PersonalSender` (pyautogui automation; mirrors `WebhookSender`'s callback/stop interface). Flow: activate WeCom window → `Ctrl+Alt+F` search → (条件等待搜索窗口) → paste name → `get_ocr().find_contact(name, unit)` → click result → (条件等待搜索窗口消失 = 已进会话) → `_paste_and_send` (text first, then files). `dry_run=True` verifies search without sending. `pyautogui.FAILSAFE` is enabled — moving the mouse to the screen top-left corner is the emergency stop. **逐条重试 + 阶段化**：每个联系人最多试 `max_attempts`（默认 2）次，**只对 `search`/`enter_chat` 失败重试**，`FailSafe` 不重试并终止整个队列；**`_stop_flag` 一置位就立刻放弃这条、不再重试**（并把 `should_stop=lambda: self._stop_flag` 透给 `find_contact`，让停止能从滚动查找中即时中断）；失败落 `stage` + `detail`（含"OCR 识别到：…"）并写 `logs/fail_*.png` 截图。`search_delay`/`chat_load_delay` 现在是**超时基准**（×4 / ×3 喂给 `_wait_search_window`），不再是固定 sleep。
- `ocr_helper.py` — `WeComOCR` wraps PaddleOCR (lazily initialized; `get_ocr()` is the module-level singleton). `find_contact(name, unit, should_stop=None)` → `_scan_and_match()` 用**几何配对**认人：`extract_text_blocks()` 返回的块已按 `(y, x)` 排序，取姓名块之后、纵向距离 ≤ `ROW_GAP_MAX`（模块级常量，默认 60）的块作为"同一条联系人"的候选，覆盖「部门在同行右侧」与「部门在下一行」两种版式（旧的"下一个块"配对会认错人）。`_wait_search_window(appear=, timeout=)` 轮询「全局搜索」窗口出现/消失，取代盲睡。`self.last_blocks` 缓存最后一次 OCR 结果供失败复盘（`find_contact` 找「查看全部」时复用它，不再重复整屏 OCR）。`capture_result_area()` locates the "全局搜索" window via `pygetwindow`. Hardcoded capture coordinates are tuned for specific screen resolutions.
  - **`_scroll_and_match` 必须保持有界**：它在「查看全部」展开的列表里逐条按 ↓ 查找，而**每次迭代都是一次整屏截图 + PaddleOCR 推理（1~3s）**。上限为双保险——次数 `SCROLL_MAX_ATTEMPTS`（默认 10）与耗时 `SCROLL_MAX_SECONDS`（默认 20s），任一先到即放弃（两个都是模块级常量，漏找人调大、等太久调小）。原先写死 `range(50)` 会让「搜不到的人」卡 2~4 分钟，观感就是"一直循环找这个人"。另外 `should_stop` 无参可调用对象会被逐轮检查，供「停止」即时中断（不经它的话一次滚动最长要等 20s 才响应停止）。
- `clipboard_helper.py` — `copy_files_to_clipboard()` writes a `DROPFILES`/`CF_HDROP` structure (UTF-16LE) so `Ctrl+V` pastes files as attachments in WeCom. Requires `pywin32`.

### `ui/` — PyQt5 panels
`MainWindow` (`ui/main_window.py`) hosts a `QTabWidget` with four tabs, wired to the config managers:
- `msg_editor.py` — `MsgEditorPanel`: template picker, message editor with `{variable}` insertion, live Markdown-ish preview.
- `group_panel.py` — `GroupPanel`: group CRUD, search/filter, CSV/Excel import/export. Validates webhook URLs against the `https://qyapi.weixin.qq.com/cgi-bin/webhook/send` prefix.
- `send_panel.py` — `SendPanel`: group webhook sending. Interval (WeCom rate limit: **20 msgs/min/robot**, default 3s), retries, target-group checkboxes, progress bar, log, history. `SendThread` (QThread subclass) runs `WebhookSender` off the UI thread; `VariableDialog` prompts for `{placeholder}` substitution before sending.
- `personal_panel.py` — `PersonalPanel`: person table with enable checkboxes, unit/tag multi-select filters (`MultiCheckFilterButton`), name search, file attachment list, send params, and a 「我的任务」table（类型筛选 `全部/个人发送/群组发送`，默认只看个人发送；`🔄 刷新`；双击行看逐条结果与失败截图）。**本面板只提交审批、不发送**，所以它没有进度条/发送日志/停止按钮（那套是"经办人直接发送"时代的残留，已于 v1.2.0 删除，别再按需加回来）。

### Login & approval flow (added 2026-08)
- `main.py` now gates startup behind a `LoginDialog` (`ui/login.py`) and routes by role: **Operator** (经办人) → existing `MainWindow`; **Approver** (审批人) → new `ApprovalWorkbench` (`ui/approval_workbench.py`). Both roles are forced to change the seeded token on first login (`ChangeTokenDialog`); either can also change it later proactively (operator: 工具 → 修改 Token; approver: 🔑 修改 Token button). `python main.py --reset-auth` restores the initial tokens. The login dialog has a **role dropdown** and `AuthManager.verify(token, role=...)` checks only the selected role, so even identical tokens across roles remain distinguishable.
- `core/auth.py` — `AuthManager`: tokens are stored only as per-role PBKDF2-SHA256 salted hashes in `data/auth.json` (never plaintext, never env vars). Hardcoded initial tokens live in `INITIAL_OPERATOR_TOKEN` / `INITIAL_APPROVER_TOKEN`. `change_token(role, token, employee_no=, sso_account=)` — the approver's forced/proactive token change requires 工号 + SSO 账号名 (enforced in `ChangeTokenDialog`), persists them in the record, and logs the change to `app.log` + audit `change_token` (audit.jsonl).
- `core/approval.py` — `PendingTaskStore` (JSON `data/pending_tasks.json`, atomic tmp+`os.replace` writes; statuses `pending → approved → sending → done|failed|rejected`) and `AuditLog` (append-only JSONL `logs/audit.jsonl` + mirrored via `logging`).
- Operator panels no longer send directly: `SendPanel.start_send` / `PersonalPanel.start_send` now serialize a task (targets, content, send_params, submitter) into `PendingTaskStore` instead. The senders (`WebhookSender`/`PersonalSender`) are untouched and reused by `ApprovalSendWorker` in the workbench, which also persists per-target `sent_targets`/`remaining_targets` so a crash mid-send can be resumed (stale `sending` tasks are offered for resume/fail on workbench startup).
- The workbench executes approved tasks through a **single persistent `QThread` + queue**: `ApprovalSendWorker.submit()` is invoked via a queued signal (`ApprovalWorkbench.submit_signal`) and serially drains a task-id queue in its `_maybe_start`/`_run_one` loop. This avoids the prior per-task `QThread` creation, which raced and left the 2nd task of a batch stuck in `sending`.

### v1.2.0 稳定性加固与失败定位 (added 2026-10)
两个目标：**自动操作企业微信更稳**、**能定位到哪一笔失败和失败原因**。刻意**不做架构改造**——Windows 服务的 Session 0 隔离决定了服务化无法做 GUI 自动化；"经办+审批双窗口"反而更不稳（两个进程共用同一个企业微信窗口/鼠标/剪贴板，且 `PendingTaskStore` 会丢更新）。单进程 + 单实例最稳。

- `main.py` — `QApplication` 建好、进入登录之前用 `QLockFile(data/app.lock)`（`setStaleLockTime(30_000)`）拿单实例锁；失败弹「本程序已在运行中」并退出。锁对象需存活到进程结束（`main()` 局部变量即可）。
- `core/approval.py` — `PendingTaskStore.update_results(task_id, results)` 把逐条结果写进任务（沿用 `save()` 原子写）。只在**任务结束时一次性写**，避免每条都全量重写 `pending_tasks.json`；崩溃续发仍靠 `sent_targets`/`remaining_targets`。
- `ApprovalSendWorker._run_one` 结束时把结果归一成纯 dict（`_result_to_dict`：`name/unit/success/stage/message/detail/attempts/elapsed/screenshot`）调用 `update_results`。
- `ui/approval_workbench.py` — 进度条下方新增「逐条结果」`QTableWidget`（✓/姓名/部门/状态/失败阶段/备注/耗时，失败行整行标红，**双击失败行弹失败截图**，每次执行开始清空）；任务列表加「待审批 / 全部」筛选（默认待审批；非待审批行不给复选框，只有 pending 可审批，approve/reject 内仍按 `status == STATUS_PENDING` 兜底）；`_show_detail` 末尾追加「发送结果」段。
- `ui/personal_panel.py` — 「我的任务」加「全部 / 个人发送 / 群组发送」类型筛选（默认个人发送）＋「🔄 刷新」按钮 + **双击任务行弹详情**（发送对象 + 逐条结果，失败行给阶段/备注/截图路径，可一键用 `os.startfile` 打开截图）。注意**不能按 `submitter` 过滤**：它是常量 `ROLE_LABELS[ROLE_OPERATOR]`（"经办人"），系统里只有一个经办人角色、`auth.json` 里经办人的 `employee_no`/`sso_account` 故意为空（"经办人改密时无需登记"），所以按人区分需要先给经办人加身份登记。
- 同批删除**"经办人直接发送"时代的残留**（否则界面上的假按钮会误导人）：`PersonalSendWorker`、`PersonalPanel` 的进度条/发送日志区/「⏹ 停止」按钮、`_on_progress`/`_on_result`/`_on_finished`/`_log`/`is_sending`/`self.sender`——这些早已无人调用（`start_send` 只写 `PendingTaskStore`），点「停止」也停不了审批人那边的发送。
- 失败证据落三处：`logs/app.log`（候选块/为什么没匹配）、`logs/personal_send_*.csv`（逐行阶段/重试/耗时/截图，12 列：`…发送状态, 失败阶段, 备注, …, 重试次数, 耗时(秒), 失败截图`）、`logs/fail_*.png`（截图，任何 `stage != done` 都存一张）。
- **失败截图必须在关窗之前拍**（v1.2.2 修）：`_search_contact` 里的 `pyautogui.press("esc")` 会关掉「全局搜索」窗口，而 `_save_failure_shot` 靠该窗口定位区域 —— 顺序反了就让截图**永远**失败（现场表现是 CSV 的「失败截图」列全空 + 日志 `保存失败截图失败（可能未打开搜索窗口）`）。现在失败路径一律先 `self._pending_shot = self._save_failure_shot(name)` 再 esc，`send_to_persons` 用 `_shot_for()` 取暂存的那张。找不到窗口时 `_save_failure_shot` **退化为全屏截图**（一定拿得到）。
- `_ocr_hint` 会把**全部** OCR 文字块（不止前 8 个）和**截图区域几何**写进失败详情 —— 认不出人时最常见的根因就是截取区域没框到部门文字，没这两个信息事后无从判断。**「单位写错」与「没认出这个人」是两种失败、排查方向相反**：`_scan_and_match` 在"姓名命中但候选块不含单位"时记 `self.last_mismatch = {name, target_unit, candidates}`（`find_contact` 开头重置），`_ocr_hint` 见到它就报「姓名已命中但单位不符（姓名=…，期望单位=…；该联系人处识别到：…）」而不是笼统的「未识别到联系人」。看到「未识别到联系人」时应查截图区域/搜索是否真的没结果，看到「单位不符」则应查数据里的单位串或该行版式。
- **`Ctrl+Alt+F` 是开关，不是"打开"**（v1.2.2 观念，v1.2.4 修正）：搜索窗已开着时再按会把它**关掉**，于是后续既截不到图也认不出人 → 表现为与单位无关的假失败。v1.2.2 的应对是"已打开就跳过这个热键"，但 v1.2.4 实测发现**每次都必须从干净的一次搜索开始**（见下节），所以 `_search_contact` 改成：若 `ocr._search_window_open()` 为真 → 先 `focus_search_window` + `press("esc")` **主动关掉残留窗**，再 `_switch_to_wecom` + 按热键**重开一个新窗**（统一走一条路径，不再有"跳过"分支）。
- **「企业微信明明开着却报未找到企业微信窗口」= 窗口缩在托盘**（v1.2.3 修）：`pygetwindow` 只枚举**可见**窗口（`_getAllTitles()` 里带 `IsWindowVisible` 过滤），而企业微信点 X 是"缩到托盘"——窗口 hidden、进程仍在，于是 `getWindowsWithTitle("企业微信")` 返回空 → 旧的硬抛 `未找到企业微信窗口，请确认企业微信已打开`。本机实测：`WXWork.exe` 在跑，顶层有 4 个 `WeWorkWindow`（title 都是「企业微信」）其中 **3 个 `visible=False`**，`pygetwindow` 只看到 1 个。修法：`_switch_to_wecom` **不再抛异常**——可见窗口找不到就用 `_find_hidden_wecom_hwnd()`（ctypes `EnumWindows`，**不筛可见性**；用 stdlib 而非 pywin32 以免动打包配置）捞隐藏窗口并 `ShowWindow(SW_RESTORE)`+`SetForegroundWindow`；连隐藏窗口都没有才只打警告。理由：真正的成败判据是「全局搜索」窗口能否出现（`_wait_search_window`），这里尽力而为即可，硬抛只会把"能发"的情况误判成失败。`_search_contact` 里原本重复的那段窗口查找也改成调 `_switch_to_wecom`。注意 `ShowWindow` 的分支：`IsIconic`（最小化）用 `SW_RESTORE=9`，只是**托盘隐藏但未最小化**的窗口 `SW_RESTORE` 无效、必须用 `SW_SHOW=5`（本机实测，见下条）。

### v1.2.4 现场可用性修复（「进不到对话界面 / 总是输入人名」）
现场症状：**企业微信开着，但搜索人员后进不去会话；即使进去了也总是把姓名输进聊天框；整体不可用**。全部在本机真机实测定位（`Ctrl+Alt+F`、点击结果、焦点归属逐条验证过），四个根因：

- **`Ctrl+Alt+F` 不是全局热键**（推翻 v1.2.3 里的猜测）：只有企业微信是**前台窗口**时它才打开「全局搜索」。所以 `_search_contact` 在按热键**之前**先调 `self._switch_to_wecom(pyautogui)`（把企业微信置前），否则搜索窗根本不出现。
- **「全局搜索」窗口必须先置前**，否则 `Ctrl+A`/`Ctrl+V` 会落到**当前拥有焦点的窗口**=聊天输入框 → 表现为「**总是输入人名**」；同时 `capture_result_area()` 截屏截到的是盖在上面的聊天内容 → 表现为「**进不到对话界面/认不出人**」。修法：新增 `WeComOCR.focus_search_window(timeout)`（`_search_hwnd()` 用 ctypes `EnumWindows` 按**标题恰好等于「全局搜索」**取句柄，`ShowWindow(SW_RESTORE)`+`SetForegroundWindow`，校验 `GetForegroundWindow()==hwnd`，失败时按一次 Alt（`keybd_event(0x12)`）解除前台锁再重试一次，仍失败则告警返回 False）。`_search_contact` 在粘贴姓名**之前**必须 `focus_search_window` 成功，失败即 `stage="search"` 记「「全局搜索」窗口无法置前（可能被其他窗口抢占）」。
- **点击搜索结果后「全局搜索」窗口不会消失**（实测），只是**失去焦点**（其页脚是「↑↓上/下一个 ↵打开 Ctrl+Tab 切换类别 Esc 关闭窗口」）。所以旧的判据 `_wait_search_window(appear=False)`（等窗口消失）**永远为 False**，把每一次成功都判成失败 → 现场「已点击联系人但未进入会话」。新增正确判据 `WeComOCR._wait_search_blur(timeout)`：轮询**搜索窗不再是前台窗口**即视为已进会话。
- **粘贴姓名后要等结果渲染**：OCR 立刻截图会只看到搜索窗的框架文字、甚至把群预览行当成联系人。`_search_contact` 在 `Ctrl+V` 后 `time.sleep(self.search_delay)` 再 `find_contact`。
- **每次都必须从"干净的一次搜索"开始**：上一轮可能点过「查看全部」把列表展开过，残留状态会让下一个联系人匹配错乱（实测：单独跑 黄云 通过、跟在别人后面跑 黄云 失败）。`_search_contact` 开头若 `ocr._search_window_open()` 为真，先 `focus_search_window` + `press("esc")` 关掉残留窗再重开。
- **`_scan_and_match` 加全块兜底**（`_match_in`）：OCR 文字与数据里的单位串**完全一致**却报不匹配时，怀疑「联系人」标题切片切掉了目标行。现在构造 `sequences`（展开→全部块；否则标题切片 `blocks[idx+1:]` **再退回全部块**），逐个 `_match_in` 匹配，任一段命中即返回，都不中再逐段记 `last_mismatch`。

**v1.2.4 真机验证结论**：刘成/正确单位 → 成功；黄云/正确单位 → 成功；黄云/**故意错单位** → 正确报「姓名已命中但单位不符」（用户此前那条错单位记录就是**故意的负向测试**，现在能准确报出而不是笼统的"未识别到联系人"）。

### Concurrency convention
Sending always runs in a background `QThread` to keep the UI responsive; progress/result/finish are delivered via `pyqtSignal`, and stopping is cooperative via the sender's `_stop_flag`. Note the two styles in use: `send_panel.py` subclasses `QThread` (`SendThread`), while `approval_workbench.py` uses the recommended moveToThread pattern (`ApprovalSendWorker` on one persistent thread + task queue). Prefer the moveToThread pattern for new code.

## Data & Runtime Files

- `data/` — JSON state (`groups.json`, `templates.json`, `persons.json`, `auth.json`, `pending_tasks.json`); the app creates/seeds them at runtime. All gitignored. **`data/` is intentionally NOT bundled into the exe** (only `models/` is) — bundling it would ship the builder's runtime state (e.g. a modified `auth.json` kills the first-login forced password change). Also holds the single-instance lock file `app.lock`（运行时文件，不影响打包）。
- `logs/` — `app.log` (app-level logging set up in `main.py`), `send.log` (sender logging set up in `sender.py`), `history.json` (send history), `audit.jsonl`（审批审计）, `personal_send_*.csv`（个人发送逐条结果，一次运行一个文件）, `fail_*.png`（逐条失败截图，仅失败时写；找不到搜索窗口时为全屏图）, `crash.log`（未捕获异常的完整 traceback，仅崩溃时写）. All gitignored.
- `零售通知工具.spec` — PyInstaller spec with `collect_all` for Paddle/PyQt stack, whole-package dir copies of native `.pyd`/`.dll` packages, and excludes (`torch`, `tensorflow`, `paddle.distributed`, …) to slim the bundle. Touch this only if the dependency set changes.

## Gotchas

- `requirements.txt` pins exact versions (`numpy==1.26.4`, `paddlepaddle==2.6.2`, `PyQt5==5.15.11`) — changing them can silently break OCR or the build.
- `SendPanel.get_message_content()` locates the message editor by walking up the Qt parent chain for an object with a `msg_editor` attribute — keep that attribute name when refactoring.
- Personal sending flows require the WeCom PC client open and logged in; `PersonalSender.check_dependencies()` guards the pyautogui/pyperclip install.
- `ocr_helper.py` contains a lot of commented-out legacy code and debug prints — treat it as exploratory. The `find_contact` / `_scan_and_match` / `_scroll_and_match` path is the active one.
- **找不到人时的第一现场是「失败截图」和失败详情，不是 app.log**：实测（v1.2.2 之前）`core.ocr_helper` 的 **INFO 日志在实际运行中不会落到 `app.log`**（同一次运行里 `core.personal_sender` 的 INFO 正常），所以 `匹配成功/姓名命中但单位不符/未匹配到联系人` 这类 INFO 诊断行**可能看不到**。**原因未查明**（普通脚本里复现不出来；不是 root/logger 级别，也不是 paddle 的 `logging.disable`）。因此诊断信息一律经 `result.detail` 带出去（`_ocr_hint` 写全块文本 + 截图区域几何 → 落 CSV/app.log 的 WARNING/ERROR），不要依赖 ocr_helper 的 INFO 行。要复现这条 INFO 消失，就在装了 OCR 的 GUI 里跑一次个人发送，然后 `grep -c "y=" logs/app.log`（0 即为复现）。
- **认错人 / 找不到人先看四处**：① `ROW_GAP_MAX`（默认 60）是否大于实际行距——调小可避免把下一行的块当成这条人的部门；② 失败详情里的**全部 OCR 文字块**（判断"部门文字到底有没有被截进来"，没有就是截图区域问题，不是配对问题）；③ `logs/fail_*.png`（现在一定会有：窗口区域图，找不到窗口时是全屏图）；④ 「找到太慢」看 `SCROLL_MAX_ATTEMPTS`/`SCROLL_MAX_SECONDS`（默认 10 次 / 20s）。改这些常量都不需要动逻辑代码。
- `_scan_and_match` 的「联系人」标题：**不再假设它是结果上方的分类标题**。实际版式里它常常是左侧/下方分类列表的**最后一项**，那样"它之后的块"是空集、匹配必然失败（现场踩过：OCR 明明认出「Q 黄云」却报"未识别到联系人"）。现在找不到标题、或标题正好在最后一块时，退回用**全部块**匹配。
- **不要在 `SendResult` 参数中段插字段**：新字段一律加在末尾（有默认值），否则 `WebhookSender`/群组流程的三参调用会错位。同理 `PersonalSender._save_results_to_csv(results, content, reviewer, content_mode)` 的**参数顺序**不能动（`PersonalSendWorker` 里有一段按位置传参的死代码）。
- **版本迭代约定**：`core/version.py` 是版本号唯一事实源（`APP_VERSION`）。每次新增功能都要递增它 —— 新功能次版本 +1（1.1.0 → 1.2.0），修 bug 修订号 +1（1.1.0 → 1.1.1）。展示点：主窗口/审批工作台标题、两处「帮助→关于」、登录窗口标题下方小字；均为引用常量，不要在别处硬编码版本串。
- 个人发送有**两种模式**（`ui/personal_panel.py` 的「统一消息 / 模板消息」单选）：统一模式忽略模板表；模板模式用 `core/personal_template.py` 解析出的 `self.template_rows`（每行 `{name, unit, content, files}`）作为 targets，忽略左侧联系人勾选。逐人消息随 target dict 一起落盘，崩溃续发天然生效；`SendResult` 的 `unit`/`content` 字段与 `_on_result` 的 name+unit 匹配是「同姓名跨部门」正确续发的前提。
- 模板模式的**附件来源**由「所有人相同 / 每人不同」单选决定：相同→用全局附件列表（任务级 `file_paths`）；每人不同→读模板「附件」列（`;` 分隔）写进 `targets[*]["file_paths"]`，任务级 `file_paths` 置空，全局附件区被禁用。`PersonalSender` 取附件：`person["file_paths"]` 为 None 才回退全局，空列表表示「此人只发文字不发附件」。
