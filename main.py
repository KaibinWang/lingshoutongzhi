"""
零售通知自动分发工具
主程序入口
"""

import sys
import os
import traceback

# 添加项目根目录到路径
if getattr(sys, 'frozen', False):
    # 打包后的路径
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # 开发环境路径
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)


# ── 打包后（GUI 子系统、无控制台）标准流兜底 ─────────────────────
# spec 里 console=False → exe 是 WINDOWS_GUI 子系统。双击启动时没有控制台可继承，
# Python 会把 sys.stdout / sys.stderr 置为 None；而 paddle / paddleocr 里有**裸写**
# sys.stdout.write(...) 的分支（paddle/base/core.py 的 AVX 提示、
# ppocr/utils/utility.py::check_install），一旦走到就是
# AttributeError: 'NoneType' object has no attribute 'write' 直接崩。
# 这里在导入任何第三方库之前把它们换成真实可写对象，根因消除。
# 指向 devnull 而不是文件：OCR 每认出一个文字块都会 print 一遍（内容与
# app.log 里的 logger.info 重复），指到文件会让日志无限膨胀。
def _ensure_std_streams() -> None:
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name, None) is None:
            try:
                setattr(sys, _name, open(os.devnull, "w"))
            except Exception:
                pass


_ensure_std_streams()

import logging
from datetime import datetime
from core.config import LOGS_DIR


# ── 崩溃取证 ──────────────────────────────────────────────────
# 打包后 Qt 槽里逃出的未捕获异常会让进程直接 abort：屏幕上什么都没有
# （无控制台时连 stderr 都不存在），缓冲区也不会 flush。装一个 excepthook
# 把完整 traceback 写进 logs/crash.log 并 flush+fsync，是现场唯一的取证手段。
def _install_crash_logger() -> None:
    def _hook(exc_type, exc_value, exc_tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            with open(LOGS_DIR / "crash.log", "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{text}")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


_install_crash_logger()

logging.basicConfig(
    filename=os.path.join(str(LOGS_DIR), "app.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    encoding="utf-8",
    force=True
)


from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox
from PyQt5.QtCore import QLockFile
from PyQt5.QtGui import QFont
from ui.main_window import MainWindow


def main():
    """主函数"""
    # 处理 --reset-auth：重置为初始凭证（遗忘 Token 后恢复用）
    if "--reset-auth" in sys.argv:
        from core.auth import get_auth
        get_auth().reset()
        print("已重置为初始 Token（详见 core/auth.py 中的 INITIAL_*_TOKEN 常量）")
        return

    app = QApplication(sys.argv)

    # 应用元信息（供窗口管理器/关于等信息使用）
    from core.version import APP_NAME, APP_VERSION
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # 设置应用程序字体
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)

    # 设置应用程序样式
    app.setStyle("Fusion")

    # ── 单实例锁 ──────────────────────────────────────────
    # 企业微信自动化天然只能有一个操作者：两个进程同时跑会互相争抢同一个
    # 企业微信窗口/鼠标/剪贴板，并对 pending_tasks.json 丢更新。
    # 锁只加在自己身上，不改变任何现有功能。
    from core.config import DATA_DIR
    app_lock = QLockFile(os.path.join(str(DATA_DIR), "app.lock"))
    app_lock.setStaleLockTime(30_000)
    if not app_lock.tryLock(100):
        QMessageBox.warning(None, "程序已在运行", "本程序已在运行中，请勿重复打开。")
        return

    # ── 登录拦截 ──────────────────────────────────────────
    from core.auth import get_auth, ROLE_APPROVER
    from ui.login import LoginDialog, ChangeTokenDialog

    auth = get_auth()

    login = LoginDialog(auth)
    if login.exec_() != QDialog.Accepted:
        return  # 用户未登录，退出

    # 首次登录强制改密（经办人 / 审批人）
    if login.needs_change:
        change = ChangeTokenDialog(auth, login.result_role)
        if change.exec_() != QDialog.Accepted:
            return

    # ── 路由/视图隔离 ─────────────────────────────────────
    if login.result_role == ROLE_APPROVER:
        from ui.approval_workbench import ApprovalWorkbench
        window = ApprovalWorkbench(auth=auth)
    else:
        window = MainWindow()

    # 默认最大化显示（主窗口与审批工作台共用此入口）
    window.showMaximized()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
