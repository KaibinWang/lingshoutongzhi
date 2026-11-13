"""版本单一事实源。
约定：每次新增功能都要递增 APP_VERSION。
  · 新功能   → 次版本 +1（1.1.0 → 1.2.0）
  · 修 bug   → 修订号 +1（1.1.0 → 1.1.1）
"""

APP_NAME = "零售通知自动分发工具"
APP_VERSION = "1.2.4"
VERSION_DISPLAY = f"{APP_NAME} v{APP_VERSION}"