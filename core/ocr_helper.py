"""
OCR 辅助模块
截取企业微信搜索结果区域，识别联系人部门信息并匹配目标单位。
"""

import os
import sys
import shutil

import pyautogui
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── 搜索结果截图区域（基于 2560x1600 分辨率）──────────────────
# 根据截图观察：搜索结果第一条大约在窗口上方 160~280px 区域
# 这里截取的是部门文字所在的那一行
# 如果识别不准，可以适当调整 TOP/BOTTOM
RESULT_LEFT   = 0
RESULT_TOP    = 155
RESULT_RIGHT  = 1400
RESULT_BOTTOM = 290

# ── 姓名块与部门块配对的最大纵向距离（像素）──────────────────
# 搜索结果里"部门"可能在姓名的右侧同一行，也可能在姓名下方的下一行。
# 取姓名块之后、且纵坐标相差不超过该值的块作为候选，覆盖两种版式。
# 识别认错人时可调小该值（例如 40）。
ROW_GAP_MAX = 60

# ── 「查看全部」展开后的滚动查找上限 ──────────────────────────
# 每按一次 ↓ 都要做一次「截图 + PaddleOCR 全量推理」（约 1~3 秒），
# 所以绝不能无限滚：原来的 50 次会让「搜不到的人」卡上 2~4 分钟，
# 观感就是"一直循环找这个人"。两个上限任一先到即放弃。
# 觉得会漏找人 → 调大；觉得等太久 → 调小。
SCROLL_MAX_ATTEMPTS = 10
SCROLL_MAX_SECONDS  = 20.0


def _search_hwnd() -> int:
    """返回标题恰好是「全局搜索」的顶层窗口句柄，找不到返回 0。

    用 ctypes 直接 EnumWindows：需要拿到**句柄**（pygetwindow 的 Window 对象
    不便参与 GetForegroundWindow 比较），且这里不筛可见性，稳一点。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _lp):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.strip() == "全局搜索":
            found.append(hwnd)
        return True

    user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(_cb), 0)
    return found[0] if found else 0


class WeComOCR:
    """企业微信搜索结果 OCR 识别器"""

    def __init__(self):
        self._ocr = None
        self.last_blocks: list = []   # 最近一次 OCR 的文字块，供失败时复盘
        self.last_region = None       # 最近一次截图区域 (left, top, w, h)，供复盘
        # 最近一次「姓名命中但单位不符」的证据 {name, candidates}，供失败详情区分
        # 「单位对不上」与「压根没认出这个人」——两者排查方向完全不同。
        self.last_mismatch = None

    # def _get_ocr(self):
    #     """懒加载 PaddleOCR，避免启动时拖慢速度"""
    #     if self._ocr is None:
    #         from paddleocr import PaddleOCR
    #         self._ocr = PaddleOCR(
    #             use_angle_cls=False,  # 搜索结果都是正向文字，不需要角度检测
    #             lang="ch",
    #             show_log=False,       # 关闭 PaddleOCR 的日志输出
    #         )
    #         logger.info("PaddleOCR 初始化完成")
    #     return self._ocr
    def _get_ocr(self):
        if self._ocr is None:
            from paddleocr import PaddleOCR

            # 优先使用随程序打包的本地模型（离线可用），避免首次运行从外网下载模型失败。
            kwargs = {"lang": "ch", "show_log": False}
            model_dirs = self._local_model_dirs()
            if all(d is not None for d in model_dirs):
                det_dir, rec_dir, cls_dir = model_dirs
                kwargs.update(
                    det_model_dir=det_dir,
                    rec_model_dir=rec_dir,
                    cls_model_dir=cls_dir,
                )
                logger.info("使用本地 OCR 模型: %s", ", ".join(os.path.basename(d) for d in model_dirs))
            else:
                logger.warning("未找到内置 OCR 模型目录，将尝试在线下载（可能因网络受限失败）")

            try:
                self._ocr = PaddleOCR(**kwargs)
            except SystemExit:
                # paddleocr 下载模型失败时会直接 sys.exit(0)，在窗口程序里会静默闪退，这里转为可读错误
                logger.error("PaddleOCR 模型初始化被中断（模型缺失或下载失败）")
                raise RuntimeError(
                    "OCR 模型初始化失败：未找到内置模型且在线下载失败。"
                    "请确认程序自带的 models 目录完整，或重新打包（build.bat 会自动下载模型）。"
                )
            except Exception as e:
                logger.error("PaddleOCR 初始化失败: %s", e)
                raise RuntimeError("PaddleOCR 初始化失败，请检查程序自带的 models 目录") from e

            logger.info("PaddleOCR 初始化完成")

        return self._ocr

    @staticmethod
    def _model_base() -> str:
        """模型根目录：frozen 时位于 sys._MEIPASS/models，开发环境位于项目根 models/。"""
        if getattr(sys, "frozen", False):
            return os.path.join(sys._MEIPASS, "models")
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
        )

    @classmethod
    def _model_complete(cls, model_dir: str) -> bool:
        return (
            os.path.isfile(os.path.join(model_dir, "inference.pdmodel"))
            and os.path.isfile(os.path.join(model_dir, "inference.pdiparams"))
        )

    @classmethod
    def _local_model_dir(cls, name: str) -> Optional[str]:
        """
        返回可被 Paddle 加载的本地模型目录；未找到（文件不完整）返回 None。

        注意：Paddle 的 C++ 推理引擎在 Windows 上无法打开含中文等非 ASCII 字符的路径，
        因此当模型所在路径含非 ASCII 字符时，会先把模型复制到 %PROGRAMDATA% 下的
        ASCII 目录（C:\\ProgramData\\LingShouOCR\\models）再返回该目录，保证离线可用。
        """
        path = os.path.join(cls._model_base(), name)
        if not cls._model_complete(path):
            return None
        if any(ord(c) > 127 for c in path):
            return cls._ensure_ascii_copy(name)
        return path

    @classmethod
    def _ensure_ascii_copy(cls, name: str) -> Optional[str]:
        """把模型复制到 ASCII 目录并返回其路径；复制失败返回 None。"""
        src = os.path.join(cls._model_base(), name)
        dst_root = os.path.join(
            os.environ.get("PROGRAMDATA", "C:/ProgramData"), "LingShouOCR", "models"
        )
        try:
            os.makedirs(dst_root, exist_ok=True)
        except OSError as e:
            logger.error("创建模型缓存目录失败 %s: %s", dst_root, e)
            return None
        dst = os.path.join(dst_root, name)
        if cls._model_complete(dst):
            return dst
        try:
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            logger.info("已将 OCR 模型复制到 ASCII 目录: %s", dst)
        except OSError as e:
            logger.error("复制 OCR 模型到 ASCII 目录失败: %s", e)
            return None
        return dst if cls._model_complete(dst) else None

    def _local_model_dirs(self):
        return (
            self._local_model_dir("ch_PP-OCRv4_det_infer"),
            self._local_model_dir("ch_PP-OCRv4_rec_infer"),
            self._local_model_dir("ch_ppocr_mobile_v2.0_cls_infer"),
        )

    def _wait_search_window(self, appear: bool = True,
                            timeout: float = 6.0, interval: float = 0.25) -> bool:
        """轮询企业微信"全局搜索"窗口出现/消失，超时返回 False。

        Args:
            appear:  True = 等待窗口出现（发出搜索后）。当前调用方只用 True；
                     False（等待窗口消失）自 v1.2.4 起**不再用于判断"已进会话"** ——
                     点击结果后搜索窗并不会消失，只是失去焦点，见 _wait_search_blur。
            timeout: 最长等待秒数（由调用方按用户参数换算，避免硬编码 sleep）
            interval: 轮询间隔秒数

        Returns:
            是否在超时内达成期望状态
        """
        import pygetwindow as gw

        deadline = time.time() + timeout
        while time.time() < deadline:
            found = any(
                w.title.strip() == "全局搜索"
                for w in gw.getAllWindows()
            )
            if found == appear:
                return True
            time.sleep(interval)
        return False

    def _search_window_open(self) -> bool:
        """单次、不等待地判断「全局搜索」窗口此刻是否已打开。

        用途：Ctrl+Alt+F 是**开关**（已开时再按会关掉），且上一轮可能点过「查看全部」
        把列表展开过、版式已变。发送前若窗口还开着，必须**先关掉再重开**，
        否则要么把新搜索窗关掉（无窗口可截 → 「搜索窗口未出现」），要么沿用被展开的
        旧版式（第一次扫描必然认不出人）。见 _search_contact 开头。
        """
        import pygetwindow as gw

        return any(w.title.strip() == "全局搜索" for w in gw.getAllWindows())

    def focus_search_window(self, timeout: float = 3.0) -> bool:
        """把「全局搜索」窗口调到**最前并取得焦点**，成功返回 True。

        为什么必须有这一步（v1.2.4 修，现场三连坑的共同根因）：
        · `Ctrl+A`/`Ctrl+V` 发给的是**当前焦点窗口**。搜索窗只是"开着"却不在前台时，
          姓名会被粘贴进企业微信主窗/聊天窗的**消息输入框** —— 现场表现就是
          "总是输入人名"（截图里搜索框和聊天框同时出现了那个人名）。
        · `capture_result_area` 截的是**屏幕像素**。搜索窗被主窗/聊天窗盖住时，
          截到的是盖在上面的那个窗口（OCR 读到"单聊 / xx群"这类聊天内容），
          于是几何配对认错、算出来的点击坐标也落不到搜索结果上 ——
          现场表现就是"点击后进不到对话界面"。

        用 ctypes 直接 SetForegroundWindow 而不是 pygetwindow.activate()：
        后者在 SetForegroundWindow 返回 0 时会抛异常，而这里"尽力置前 + 校验"更可控。
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        deadline = time.time() + timeout
        while time.time() < deadline:
            hwnd = _search_hwnd()
            if not hwnd:
                time.sleep(0.15)
                continue
            user32.ShowWindow(hwnd, 9)                  # SW_RESTORE：保证可见
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.25)
            if user32.GetForegroundWindow() == hwnd:
                return True
            # SetForegroundWindow 常因"前台锁"失败；先敲一下 Alt 释放锁再重试一次
            user32.keybd_event(0x12, 0, 0, 0)           # Alt 按下
            user32.keybd_event(0x12, 0, 2, 0)           # Alt 抬起
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.25)
            if user32.GetForegroundWindow() == hwnd:
                return True
            logger.warning("「全局搜索」窗口未能置前（hwnd=%s），粘贴/截图可能落到别的窗口",
                           hwnd)
            return False
        logger.warning("未找到「全局搜索」窗口，无法置前")
        return False

    def search_window_is_foreground(self) -> bool:
        """「全局搜索」窗口此刻是否是**前台窗口**。"""
        import ctypes

        hwnd = _search_hwnd()
        return bool(hwnd) and ctypes.windll.user32.GetForegroundWindow() == hwnd

    def _wait_search_blur(self, timeout: float = 3.0, interval: float = 0.15) -> bool:
        """等待「全局搜索」窗口**失去前台焦点**，超时返回 False。

        这是"是否已进入会话"的正确判据。现场实测（v1.2.4）：点击搜索结果会打开会话
        并把焦点交给会话窗口，但「全局搜索」窗口**并不会自己关掉** —— 它只是失去焦点、
        留在原地。所以旧的"等搜索窗消失"（_wait_search_window(appear=False)）永远为 False，
        把成功当成失败（现场表现：「已点击联系人但未进入会话」，且重试时姓名被打进聊天框）。
        返回 True 表示焦点已经离开搜索窗（会话窗口已取得焦点）。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.search_window_is_foreground():
                return True
            time.sleep(interval)
        return False

    def capture_result_area(self) -> object:
        """根据企业微信窗口实际位置截取搜索结果区域"""
        import pygetwindow as gw

        windows = [
            w for w in gw.getAllWindows()
            if w.title.strip() == "全局搜索"
        ]

        if not windows:
            raise RuntimeError("未找到全局搜索窗口")

        win = windows[0]

        left = win.left
        top = win.top

        # 只截左边约75%的宽度，排除右侧名片图标所在区域
        width = int(win.width * 0.75)
        height = win.height

        region = (left, top, width, height)
        screenshot = pyautogui.screenshot(region=region)
        # 记下几何：认不出人时最常见的根因就是这块区域没框到部门文字。
        # 不打日志（每次扫描都打会刷屏），由失败详情 _ocr_hint 带出去。
        self.last_region = region
        return screenshot, region

    def read_department(self) -> str:
        """
        截图并识别搜索结果第一条的部门文字。
        返回识别到的完整文字，识别失败返回空字符串。
        """
        try:
            screenshot = self.capture_result_area()

            # 转为 numpy array 供 PaddleOCR 使用
            import numpy as np
            img_array = np.array(screenshot)

            ocr = self._get_ocr()
            result = ocr.ocr(img_array, cls=False)

            if not result or not result[0]:
                logger.warning("OCR 未识别到任何文字")
                return ""

            # 把所有识别到的文字拼在一起
            texts = [line[1][0] for line in result[0] if line[1][1] > 0.5]
            full_text = " ".join(texts)
            logger.info(f"OCR 识别结果: {full_text}")
            return full_text

        except Exception as e:
            logger.error(f"OCR 识别失败: {e}")
            return ""

    def match_unit(self, unit: str) -> bool:
        """
        识别搜索结果区域的文字，判断是否包含目标单位。

        Args:
            unit: 目标单位名称，如"信用卡中心"

        Returns:
            True = 匹配成功，可以回车进入聊天
            False = 不匹配，需要跳过
        """
        if not unit:
            # 没有填单位，不做验证，直接通过
            return True

        text = self.read_department()
        if not text:
            # OCR 识别失败，记录警告但放行，避免误杀
            logger.warning(f"OCR 识别失败，跳过单位验证，直接发送")
            return True

        matched = unit in text
        if matched:
            logger.info(f"单位匹配成功: {unit} in {text}")
        else:
            logger.warning(f"单位不匹配: 期望={unit}, 识别到={text}")
        return matched
    
    def extract_text_blocks(self):
        """
        OCR识别搜索结果区域，返回文字块及坐标
        """

        screenshot, region = self.capture_result_area()

        import numpy as np

        img_array = np.array(screenshot)


        ocr = self._get_ocr()

        result = ocr.ocr(img_array, cls=False)
        #test
        if not result:
            print("OCR 空结果")
            logger.info("OCR 空结果")
            self.last_blocks = []
            return []

        print("\n===== OCR 识别结果 =====")
        logger.info("===== OCR 识别结果 =====")

        for line in result[0]:
            text = line[1][0]
            score = line[1][1]
            print(f"{text} ({score:.2f})")
            logger.info(f"{text} ({score:.2f})")

        print("========================\n")
        logger.info("========================\n")
        #test

        if not result or not result[0]:
            logger.warning("OCR 未识别到任何文字")
            self.last_blocks = []
            return []

        blocks = []

        for line in result[0]:

            try:
                box = line[0]

                text = line[1][0].strip()

                score = line[1][1]

                if score < 0.6:
                    continue

                center_x = sum(p[0] for p in box) / 4
                center_y = sum(p[1] for p in box) / 4

                blocks.append({
                    "text": text,
                    "x": region[0] + center_x,
                    "y": region[1] + center_y
                })

            except Exception as e:
                logger.error(f"OCR解析失败: {e}")

        # 按从上到下、从左到右排序。必须排序：_scan_and_match 依赖"姓名块的后续块"
        # 找部门，未排序时块顺序是 OCR 返回顺序，会认错人/找不到人。
        blocks.sort(key=lambda b: (b["y"], b["x"]))

        logger.info("========== OCR结果 ==========")

        for block in blocks:
            logger.info(
                f"y={block['y']:.0f} x={block['x']:.0f} text={block['text']}"
            )

        logger.info("============================")

        # 缓存供失败时复盘（PersonalSender 失败详情里打印）
        self.last_blocks = blocks

        return blocks

    # def find_contact(self, target_name: str, target_unit: str):
    #     blocks = self.extract_text_blocks()
    #     if not blocks:
    #         return None

    #     # 找最后一个"联系人"分类标题
    #     contact_section_idx = None
    #     for i, b in enumerate(blocks):
    #         if b["text"].strip() == "联系人":
    #             contact_section_idx = i

    #     print(f"联系人标题在第 {contact_section_idx} 个block")

    #     if contact_section_idx is None:
    #         return None

    #     blocks_after = blocks[contact_section_idx + 1:]

    #     print("===== blocks_after =====")
    #     for i, b in enumerate(blocks_after):
    #         print(f"  [{i}] text={b['text']}")
    #     print("========================")

    #     for i, b in enumerate(blocks_after):
    #         if target_name in b["text"]:
    #             if i + 1 >= len(blocks_after):
    #                 continue
    #             next_block = blocks_after[i + 1]
    #             print(f"姓名匹配: [{i}]{b['text']} → 下一块[{i+1}]{next_block['text']}")
    #             # 改成 j, bb，避免覆盖外层的 i, b
    #             for j, bb in enumerate(blocks_after):
    #                 print(f"  [{j}] text={bb['text']}  x={bb['x']:.0f}  y={bb['y']:.0f}")
    #             print(f">>> 开始判断: target_unit='{target_unit}' next_block_text='{next_block['text']}'")
    #             print(f">>> 判断结果: {target_unit in next_block['text']}")
    #             if target_unit in next_block["text"]:
    #                 print(f">>> 匹配成功，准备返回坐标")
    #                 return {"click_x": b["x"], "click_y": b["y"]}
    #             else:
    #                 print(f">>> 单位不符，跳过，继续找下一个")
    #                 continue

    #     return None
    def find_contact(self, target_name: str, target_unit: str, should_stop=None):
        """
        Args:
            should_stop: 可选无参可调用对象；返回 True 时立即放弃（供外部"停止"用）。
        """
        # 每次查找先清空上一笔的「姓名命中但单位不符」证据，避免张冠李戴
        self.last_mismatch = None

        # 第一次扫描
        result = self._scan_and_match(target_name, target_unit)
        if result:
            return result

        # 没找到，检查是否有"查看全部"。
        # _scan_and_match 刚做过一次 OCR，直接复用其缓存结果，避免再整屏推理一次。
        blocks = self.last_blocks or self.extract_text_blocks()
        see_all_block = next((b for b in blocks if "查看全部" in b["text"]), None)

        if not see_all_block:
            logger.warning(f"未找到联系人且无'查看全部': {target_name}")
            return None

        # 点击"查看全部"展开
        logger.info(f"点击'查看全部'展开联系人列表")
        import pyautogui as _pag
        _pag.click(see_all_block["x"], see_all_block["y"])
        import time
        time.sleep(1.0)

        # 展开后滚动扫描（有次数/耗时双上限，不会无限滚）
        return self._scroll_and_match(target_name, target_unit, should_stop=should_stop)


    def _scan_and_match(self, target_name: str, target_unit: str, expanded: bool = False):
        """单次OCR扫描并匹配（几何配对）

        blocks 已在 extract_text_blocks 中按 (y, x) 排序，因此"姓名块之后的块"
        即"该姓名下方/同行右侧的块"。取纵坐标与姓名块相差 ≤ ROW_GAP_MAX 的块
        作为候选人（覆盖"部门在同一行右侧"与"部门在下一行"两种版式）。

        expanded: True表示已点击"查看全部"展开，False表示初始搜索结果
        """
        blocks = self.extract_text_blocks()
        if not blocks:
            return None

        if expanded:
            # 展开后没有Tab标签，直接从第一个block开始匹配
            sequences = [blocks]
        else:
            # 初始搜索结果里原本假设"联系人"是位于结果上方的**分类标题**，
            # 用其之后的块做匹配。但实际版式里"联系人"常常是左侧/下方分类列表的
            # 最后一项 —— 那样"它之后的块"就是空集/缺行，会漏人。
            # 所以：先按"标题之后的块"找一遍，**找不到再退回全部块重试一遍**
            # （切片有风险，退回全部块是兜底 —— 现场踩过：OCR 明明认出
            # 「黄云(huangyun)」+ 下一行部门，却因为切片漏掉了这几行而报未识别到）。
            contact_section_idx = None
            for i, b in enumerate(blocks):
                if b["text"].strip() == "联系人":
                    contact_section_idx = i
            if contact_section_idx is None or contact_section_idx >= len(blocks) - 1:
                logger.warning(
                    "'联系人'标题不可用(idx=%s, 共%d块)，改用全部块匹配",
                    contact_section_idx, len(blocks),
                )
                sequences = [blocks]
            else:
                sequences = [blocks[contact_section_idx + 1:], blocks]

        for seq in sequences:
            hit = self._match_in(seq, target_name, target_unit)
            if hit:
                return hit

        logger.info(
            "未匹配到联系人: 姓名=%s 单位=%s 全部块=%s",
            target_name, target_unit, [b["text"] for b in blocks],
        )
        return None

    def _match_in(self, seq, target_name: str, target_unit: str):
        """在给定块序列里做几何配对，命中返回 {click_x, click_y}，否则 None。

        单位为空 → 只看姓名；否则要求"姓名块之后、纵向距离 ≤ ROW_GAP_MAX 的块"
        里有任一含目标单位（覆盖"部门在同行右侧"与"部门在下一行"两种版式）。
        """
        for i, b in enumerate(seq):
            if target_name not in b["text"]:
                continue
            candidates = [
                c for c in seq[i + 1:]
                if c["y"] - b["y"] <= ROW_GAP_MAX
            ]
            if (not target_unit) or any(target_unit in c["text"] for c in candidates):
                logger.info(
                    "匹配成功: %s → 姓名块=%s 候选=%s",
                    target_name, b["text"], [c["text"] for c in candidates],
                )
                return {"click_x": b["x"], "click_y": b["y"]}
            logger.info(
                "姓名命中但单位不符: %s（期望单位=%s）候选=%s",
                b["text"], target_unit, [c["text"] for c in candidates],
            )
            # 留证据：姓名确实认出来了，只是单位在候选块里没匹配上。
            # 这能把"单位写错/版式没框到部门"与"根本没认出这个人"分开报。
            self.last_mismatch = {
                "name": b["text"],
                "target_unit": target_unit,
                "candidates": [c["text"] for c in candidates],
            }
        return None


    def _scroll_and_match(self, target_name: str, target_unit: str, should_stop=None):
        """在「查看全部」展开的列表里逐条向下查找。

        次数（SCROLL_MAX_ATTEMPTS）与耗时（SCROLL_MAX_SECONDS）双上限，
        任一先到就放弃——每次迭代都要做一次整屏 OCR，无限滚会卡很久。
        """
        import pyautogui as _pag
        import time

        started = time.time()

        for attempt in range(SCROLL_MAX_ATTEMPTS):
            if should_stop is not None and should_stop():
                logger.info("滚动查找被外部停止: %s", target_name)
                return None

            result = self._scan_and_match(target_name, target_unit, expanded=True)
            if result:
                return result

            if time.time() - started >= SCROLL_MAX_SECONDS:
                break

            # 已达次数上限：不会再有下一次扫描，不必多按一次 ↓
            if attempt == SCROLL_MAX_ATTEMPTS - 1:
                break

            logger.info(f"第{attempt+1}次按↓查找（已用 {time.time() - started:.1f}s）")
            _pag.press("down")
            time.sleep(0.3)  # 等待高亮切换

        logger.warning(
            "展开列表滚动查找后仍未找到: %s（%d 次上限 / %.1fs 上限，实际 %.1fs）",
            target_name, SCROLL_MAX_ATTEMPTS, SCROLL_MAX_SECONDS, time.time() - started,
        )
        return None
# ── 单例，避免重复初始化 PaddleOCR ────────────────────────────
_wecom_ocr = None

def get_ocr() -> WeComOCR:
    global _wecom_ocr
    if _wecom_ocr is None:
        _wecom_ocr = WeComOCR()
    return _wecom_ocr