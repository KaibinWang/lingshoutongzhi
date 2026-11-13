"""
企业微信个人发送模块
通过模拟键盘操作，在企业微信 PC 端逐个搜索联系人并发送消息。

原理：Ctrl+F 搜索联系人 → 选定结果 → 回车进入聊天 → 粘贴消息 → 发送

与 WebhookSender 保持一致的接口设计，复用 SendResult 类。
"""

import time
import logging
from datetime import datetime
from typing import List, Dict, Callable, Optional

from core.sender import SendResult, STAGE_LABELS
from core.config import LOGS_DIR
import traceback


logger = logging.getLogger(__name__)


# ── 发送参数默认值（可在 UI 中覆盖） ──────────────────────
DEFAULT_SEARCH_DELAY        = 1.5   # 搜索超时基准（秒）：×4 作为等待搜索结果窗口的最长时间
DEFAULT_CHAT_LOAD_DELAY     = 1.0   # 进入聊天超时基准（秒）：×3 作为等待搜索窗口消失的最长时间
DEFAULT_SEND_INTERVAL       = 2.0   # 每条消息发完后等待（秒）
DEFAULT_COOLDOWN_AFTER_SEND = 1.0   # 发送后冷却（秒）
DEFAULT_MAX_ATTEMPTS        = 2     # 每个联系人最多尝试次数（仅 search/enter_chat 阶段失败重试）


def _require_pyautogui():
    """懒加载 pyautogui，避免在无 GUI 环境下 import 报错"""
    try:
        import pyautogui
        import pyperclip
        return pyautogui, pyperclip
    except ImportError as e:
        raise ImportError(
            "个人发送功能需要安装额外依赖，请运行：\n"
            "  pip install pyautogui pyperclip"
        ) from e


def _find_hidden_wecom_hwnd():
    """找出**不可见**的企业微信顶层窗口句柄（点了 X 缩到托盘时主窗口就是这种）。

    pygetwindow 只枚举可见窗口（其 _getAllTitles 带 IsWindowVisible 过滤），
    所以窗口一进托盘它就"看不见"了 —— 这正是"企业微信明明开着，却报
    未找到企业微信窗口"的原因。这里用 ctypes 直接 EnumWindows（不筛可见性）。
    用 ctypes 而非 pywin32：stdlib 无需额外打包配置。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if "企业微信" in buf.value and not user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(_cb)
    user32.EnumWindows(proc, 0)
    return found[0] if found else None


class PersonalSender:
    """
    企业微信个人消息发送器

    通过 pyautogui 模拟键盘操作发送消息，接口设计与 WebhookSender 对齐。
    """

    def __init__(
        self,
        search_delay: float       = DEFAULT_SEARCH_DELAY,
        chat_load_delay: float    = DEFAULT_CHAT_LOAD_DELAY,
        send_interval: float      = DEFAULT_SEND_INTERVAL,
        cooldown_after_send: float = DEFAULT_COOLDOWN_AFTER_SEND,
        dry_run: bool             = False, # only search no sending
        max_attempts: int         = DEFAULT_MAX_ATTEMPTS,
    ):
        self.search_delay        = search_delay
        self.chat_load_delay     = chat_load_delay
        self.send_interval       = send_interval
        self.cooldown_after_send = cooldown_after_send
        self.dry_run             = dry_run
        self.max_attempts        = max(1, int(max_attempts))

        self.is_running  = False
        self._stop_flag  = False
        # 本次尝试已存的失败截图路径（必须在关窗口前截，见 _search_contact）
        self._pending_shot = ""

    # ── 私有操作 ──────────────────────────────────────────

    def _switch_to_wecom(self, pyautogui):
        """尽量把企业微信调到前台。

        **不抛异常**：企业微信点 X 是"缩到托盘"（窗口 hidden、进程仍在），
        此时 pygetwindow 枚举不到它（它只枚举可见窗口）。旧的硬抛
        「未找到企业微信窗口，请确认企业微信已打开」就是被这种情况误触发的。
        而且**Ctrl+Alt+F 不是全局热键**（实测：企业微信不在前台时按它毫无反应），
        真正的成败判据是「全局搜索」窗口能否出现（见 _search_contact /
        _wait_search_window），这里尽力而为即可。
        """
        import pygetwindow as gw

        windows = gw.getWindowsWithTitle("企业微信")
        if windows:
            win = windows[0]
            win.restore()    # 如果最小化了，先还原
            win.activate()   # 激活窗口（置顶+获取焦点）
            time.sleep(0.8)  # 等窗口激活完成
            return

        # 没有可见窗口 → 多半缩在托盘，用 ctypes 把隐藏窗口捞出来还原
        hwnd = _find_hidden_wecom_hwnd()
        if hwnd:
            import ctypes
            logger.warning("企业微信主窗口不可见（可能已缩到托盘），尝试还原 hwnd=%s", hwnd)
            if ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE（已最小化）
            else:
                ctypes.windll.user32.ShowWindow(hwnd, 5)      # SW_SHOW（实测能把托盘隐藏窗口显示出来）
            ctypes.windll.user32.SetForegroundWindow(hwnd)    # 失败也无妨
            time.sleep(0.8)
            return

        logger.warning("未找到企业微信窗口（可能未启动）；仍继续，成败以"
                       "「全局搜索」窗口是否出现为准")

    def _search_contact(self, name: str, unit: str, selector: Optional[str], pyautogui, pyperclip):
        """
        搜索联系人，用 OCR 验证单位后进入聊天。

        流程：
            关掉残留搜索窗 → 企业微信置前 → Ctrl+Alt+F 打开搜索 → 等搜索窗口就绪
            → 把搜索窗置前取焦点 → 粘贴姓名 → 等结果渲染
            → OCR 读取结果块，按几何位置配对姓名与部门 → 匹配单位 → 点击进入聊天
            → 确认焦点已离开搜索窗（=已进入会话）

        Returns:
            (ok: bool, stage: str, detail: str)
            stage 取值见 core.sender.STAGE_LABELS：search / enter_chat / done
        """
        from core.ocr_helper import get_ocr

        ocr = get_ocr()

        # 关掉上一轮残留的搜索窗，保证每次都是"干净的一次搜索"。
        # 为什么必须关：上一轮找不到人时会点「查看全部」把列表展开过，
        # 直接复用同一个窗口，下一次看到的版式完全不同 → 第一次扫描必然认不出人
        # （现场实测：同一条记录单独跑能成功，紧跟在另一条之后跑就失败）。
        # 先 focus 再 Esc：Esc 只对当前焦点窗口生效，不聚焦就等于没按。
        if ocr._search_window_open():
            ocr.focus_search_window(timeout=1.5)
            pyautogui.press("esc")
            time.sleep(0.4)

        # 打开搜索框。**Ctrl+Alt+F 不是全局热键**（实测：企业微信不在前台时按它
        # 完全没有反应），所以先把企业微信调到前台再按。
        self._switch_to_wecom(pyautogui)
        pyautogui.hotkey("ctrl", "alt", "f")

        # 条件等待"全局搜索"窗口出现（替代固定 sleep；超时基准=用户参数 search_delay×4）
        if not ocr._wait_search_window(appear=True, timeout=self.search_delay * 4):
            logger.warning("搜索窗口未出现: %s", name)
            self._pending_shot = self._save_failure_shot(name)   # 先取证
            pyautogui.press("esc")
            return False, "search", "搜索窗口未出现（企业微信未响应）"

        # 关键一步（v1.2.4）：把搜索窗调到**最前并取得焦点**。
        # 不做的话有两个后果：① Ctrl+A/Ctrl+V 发给焦点窗口，搜索窗不在前台时
        # 姓名会被打进聊天输入框（「总是输入人名」）；② 截图截的是屏幕像素，
        # 搜索窗被主窗/聊天窗盖住时截到的是聊天内容 → 认错人、点击坐标错位
        # （「点击后进不到对话界面」）。
        if not ocr.focus_search_window(timeout=max(2.0, float(self.search_delay) * 2)):
            logger.warning("「全局搜索」窗口无法置前: %s", name)
            self._pending_shot = self._save_failure_shot(name)
            pyautogui.press("esc")
            return False, "search", "「全局搜索」窗口无法置前（可能被其他窗口抢占）"

        # 粘贴姓名（用粘贴代替逐字输入，避免中文乱码）
        pyperclip.copy(name)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "v")

        # 等搜索结果渲染完再截图。粘贴之后 PaddleOCR 立刻就截图的话，
        # 结果列表可能还没渲染出来 —— 现场表现是「未识别到联系人」，
        # 或者把群聊预览行（"包含：某某、某某(9)"）当成姓名块参与配对。
        time.sleep(self.search_delay)

        # should_stop：让「停止」能在滚动查找过程中立即生效（否则最多要等 20s）
        contact = ocr.find_contact(name, unit, should_stop=lambda: self._stop_flag)

        if not contact:
            logger.warning(f"未找到联系人: {name} / {unit}")
            # 必须先截图再按 esc：esc 会把「全局搜索」窗口关掉，
            # 之后再截就找不到窗口了（这曾导致「失败截图」列永远是空的）。
            self._pending_shot = self._save_failure_shot(name)
            pyautogui.press("esc")
            time.sleep(0.3)
            return False, "search", self._ocr_hint(ocr, name, unit)

        pyautogui.click(
            contact["click_x"],
            contact["click_y"]
        )

        # 判据=焦点离开搜索窗（**不是**"搜索窗消失"）。
        # 现场实测：点击结果会打开会话并把焦点交给会话窗口，而「全局搜索」窗口
        # 并不会自己关掉（它只是失去焦点、留在原地）。用旧的"等搜索窗消失"
        # 会把成功一律判成失败（「已点击联系人但未进入会话」），进而重试、
        # 把姓名打进聊天框。
        if not ocr._wait_search_blur(timeout=self.chat_load_delay * 3):
            logger.warning("已点击但未进入会话（焦点仍在搜索窗）: %s / %s", name, unit)
            self._pending_shot = self._save_failure_shot(name)
            return False, "enter_chat", "已点击联系人但未进入会话（焦点仍在搜索窗）"

        return True, "done", ""

    @staticmethod
    def _ocr_hint(ocr, name: str, unit: str) -> str:
        """把最后一次 OCR 识别到的文字块 + 截图区域几何拼成可读提示，供失败原因复盘。

        带上「截图区域/屏幕尺寸」是必须的：认不出人的最常见原因是**截取区域不对**
        （识别到的一堆文字里根本没有部门），只有把几何信息记下来才能事后判断。
        """
        blocks = getattr(ocr, "last_blocks", None) or []
        # 全部块都要带上：部门文字可能排在第 9 块之后，只取前 8 块会看不到它
        seen = " / ".join(b.get("text", "") for b in blocks[:40])
        region = getattr(ocr, "last_region", None)
        geo = f"；截图区域={region}" if region else ""
        # 姓名确实认出来了、只是单位没对上 → 报「单位不符」（排查方向：数据写错 /
        # 版式没框到部门），不要和「根本没认出这个人」混为一谈（后者查截图区域/搜索无结果）。
        mismatch = getattr(ocr, "last_mismatch", None)
        if mismatch:
            cand = (" / ".join(mismatch.get("candidates") or [])
                    or "（该联系人处未识别到可配对的文字块）")
            return (f"姓名已命中但单位不符（姓名={mismatch.get('name', name)}，"
                    f"期望单位={unit or '（未指定）'}；该联系人处识别到：{cand}）{geo}")
        return (f"未识别到联系人（姓名={name} 单位={unit}）；"
                f"OCR 识别到：{seen or '（无文字）'}{geo}")

    def _save_failure_shot(self, name: str) -> str:
        """存一张失败现场图到 logs/fail_*.png，返回路径；失败返回空串。

        优先截「全局搜索」窗口区域——与 OCR 识别的是同一块图，最能说明
        "为什么没认出这个人"。窗口已关/找不到时退化为**全屏**截图：全屏虽大，
        但一定拿得到，是排查认人问题时唯一可靠的证据。
        """
        try:
            import pyautogui

            try:
                from core.ocr_helper import get_ocr

                screenshot, _region = get_ocr().capture_result_area()
            except Exception as e:
                logger.warning("截取搜索窗口区域失败(%s)，退化为全屏截图", e)
                screenshot = pyautogui.screenshot()

            safe = "".join(c for c in str(name) if c.isalnum() or c in "_-") or "unknown"
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = LOGS_DIR / f"fail_{ts}_{safe}.png"
            screenshot.save(str(path))
            logger.warning("失败截图已保存: %s", path)
            return str(path)
        except Exception as e:
            logger.warning("保存失败截图失败: %s", e)
            return ""

    def _shot_for(self, name: str) -> str:
        """取本次尝试已存的失败截图；没有就现截一张（窗口已关则退化为全屏）。"""
        return self._pending_shot or self._save_failure_shot(name)
 

    # def _paste_and_send(self, message: str, pyautogui, pyperclip):
    #     """将消息粘贴到输入框并发送"""
    #     pyperclip.copy(message)
    #     time.sleep(0.2)
    #     pyautogui.hotkey("ctrl", "v")
    #     time.sleep(0.3)
    #     pyautogui.press("enter")
    #     time.sleep(self.cooldown_after_send)

    def _paste_and_send(self, message: str, file_paths: list, pyautogui, pyperclip):
        """
        先发文字消息，再发文件。
        每次发送之间留足够的间隔等企业微信处理。

        Args:
            message:    文字消息内容
            file_paths: 文件路径列表，空列表则只发文字
            pyautogui:  pyautogui 模块
            pyperclip:  pyperclip 模块
        """
        import time

        # ── 第一步：发文字 ──────────────────────────────────────
        if message:
            pyperclip.copy(message)
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.3)
            pyautogui.press("enter")
            time.sleep(self.cooldown_after_send)

        # ── 第二步：逐个发文件 ──────────────────────────────────
        if file_paths:
            from core.clipboard_helper import copy_files_to_clipboard
            ok = copy_files_to_clipboard(file_paths)
            if ok:
                time.sleep(0.3)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.8)   # 等企业微信加载文件预览
                pyautogui.press("enter")
                time.sleep(self.cooldown_after_send)
            else:
                logger.warning("文件复制到剪贴板失败，跳过文件发送")

    # ── 公开接口 ──────────────────────────────────────────

    def send_to_persons(
        self,
        persons: List[Dict],
        content: str,
        file_paths: list = None,
        progress_callback: Optional[Callable] = None,
        result_callback:   Optional[Callable] = None,
        reviewer: str = "",
        content_mode: str = "统一",
    ) -> List[SendResult]:
        """
        批量发送消息到多个联系人。

        Args:
            persons: 联系人列表，每项包含 name（姓名）和 selector（可选，
                     字符串数字表示选第几条搜索结果，None 表示选第 1 条）
            content: 消息内容（纯文本，企业微信个人会话不支持 Markdown）
            progress_callback: 进度回调 callback(current, total, name)
            result_callback:   结果回调 callback(result: SendResult)

        Returns:
            发送结果列表（复用 SendResult）
        """
        pyautogui, pyperclip = _require_pyautogui()
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE    = 0.3

        results: List[SendResult] = []
        total = len(persons)
        self.is_running = True
        self._stop_flag = False

        logger.info(f"开始发送消息到 {total} 位联系人")

        for i, person in enumerate(persons):
            if self._stop_flag:
                logger.info("发送已停止")
                break

            name     = person.get("name", "未知")
            unit     = person.get("unit", "")
            selector = person.get("selector")   # None 或数字字符串
            # 模板消息：每个人自带内容；统一消息：回退到公共 content
            row_content = person.get("content") or content
            # 每人附件：模板「附件」列优先（含空列表=明确不发）；
            # 未提供该字段则回退到全局附件列表
            row_files = person.get("file_paths")
            if row_files is None:
                row_files = file_paths or []

            if progress_callback:
                progress_callback(i + 1, total, name)

            logger.info(f"[{i+1}/{total}] 正在发送到: {name}")

            start_ts = time.time()
            result = None
            attempts = 0

            # 逐条重试：仅 search / enter_chat 阶段失败重试；成功路径无额外等待
            for attempt in range(1, self.max_attempts + 1):
                attempts = attempt
                stage, detail = "search", ""
                self._pending_shot = ""       # 每次尝试重置失败截图为空
                try:
                    self._switch_to_wecom(pyautogui)
                    ok, stage, detail = self._search_contact(name, unit, selector, pyautogui, pyperclip)

                    if not ok:
                        logger.warning(f"第{attempt}次尝试失败: {name} / {detail}")
                        if self._stop_flag:
                            break           # 已停止 → 不再重试
                        if attempt < self.max_attempts and stage in ("search", "enter_chat"):
                            continue        # 可重试阶段 → 再来一次
                        break               # 重试用尽 / 不可重试阶段

                    if self.dry_run:
                        time.sleep(0.5)
                        result = SendResult(
                            name, True, "[测试] 已找到联系人，未发送",
                            unit=unit, content=row_content, stage="done",
                            attempts=attempt, elapsed=time.time() - start_ts,
                        )
                        logger.info(f"✓ [DRY RUN] {name} - 找到但未发送")
                    else:
                        self._paste_and_send(row_content, row_files, pyautogui, pyperclip)
                        result = SendResult(
                            name, True, "发送成功",
                            unit=unit, content=row_content, stage="done",
                            attempts=attempt, elapsed=time.time() - start_ts,
                        )
                        logger.info(f"✓ {name} - 发送成功")
                    break

                except Exception as e:
                    logger.error(traceback.format_exc())

                    if "FailSafe" in type(e).__name__:
                        # 紧急停止：不重试，终止整个队列
                        result = SendResult(
                            name, False, "鼠标触发紧急停止", unit=unit, content=row_content,
                            stage="send", detail="用户将鼠标移到屏幕左上角触发紧急停止",
                            attempts=attempt, screenshot=self._shot_for(name),
                            elapsed=time.time() - start_ts,
                        )
                        self._stop_flag = True
                        break

                    # 窗口未找到 / 粘贴发送异常：落阶段与原因，可重试阶段继续
                    if not detail:
                        detail = f"错误: {e}"
                    if stage not in ("search", "enter_chat"):
                        stage = "send"
                    if attempt < self.max_attempts and stage in ("search", "enter_chat"):
                        continue
                    break

            if result is None:
                # 重试用尽 / 中途被停止 → 逐条失败结果（含阶段/原因/截图）
                msg = f"发送失败（{STAGE_LABELS.get(stage, stage or '未知')}）"
                result = SendResult(
                    name, False, msg, unit=unit, content=row_content,
                    stage=stage, detail=detail or msg, attempts=attempts,
                    screenshot=self._shot_for(name),
                    elapsed=time.time() - start_ts,
                )
                logger.error(f"✗ {name} - {result.detail}")

            results.append(result)
            if result_callback:
                result_callback(result)

            # 发送间隔（最后一条不等待）
            if i < total - 1 and not self._stop_flag:
                time.sleep(self.send_interval)

        self.is_running = False
        success_count = sum(1 for r in results if r.success)
        logger.info(f"发送完成: {success_count}/{len(results)} 成功")

        # 自动记录发送结果到CSV
        self._save_results_to_csv(results, content, reviewer, content_mode)

        return results

    def _save_results_to_csv(self, results: List[SendResult], content: str,
                             reviewer: str = "", content_mode: str = "统一"):
        """自动保存发送结果到CSV文件（每次生成新文件）

        每行消息取该行自己的内容（模板模式逐人不同），统一模式回退公共 content。
        """
        import csv

        # 生成带时间戳的文件名
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = LOGS_DIR / f"personal_send_{timestamp_str}.csv"

        with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)

            # 写入表头
            writer.writerow([
                "发送时间", "联系人姓名", "所在部门", "发送状态", "失败阶段", "备注",
                "消息内容", "消息来源", "审批人", "重试次数", "耗时(秒)", "失败截图",
            ])

            # 写入每条记录
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            for result in results:
                status = "成功" if result.success else "失败"
                row_content = result.content or content
                content_preview = row_content[:100] + "..." if len(row_content) > 100 else row_content
                # 失败阶段/截图只在失败行填（列语义即"失败"相关）
                stage_text = "" if result.success else STAGE_LABELS.get(result.stage, result.stage or "")
                writer.writerow([
                    timestamp,
                    result.group_name,
                    result.unit,
                    status,
                    stage_text,
                    result.detail or result.message,
                    content_preview,
                    content_mode,
                    reviewer,
                    max(0, (result.attempts or 1) - 1),   # 重试次数 = 尝试次数 - 1
                    f"{result.elapsed:.1f}",
                    "" if result.success else (result.screenshot or ""),
                ])

    def stop(self):
        """停止发送"""
        self._stop_flag = True
        logger.info("正在停止个人发送...")

    @staticmethod
    def check_dependencies() -> tuple:
        """
        检查依赖是否已安装。

        Returns:
            (ok: bool, message: str)
        """
        try:
            import pyautogui   # noqa: F401
            import pyperclip   # noqa: F401
            return True, "依赖已安装"
        except ImportError as e:
            return False, f"缺少依赖: {e}\n请运行: pip install pyautogui pyperclip"