"""
VSpider 配置管理模块

从 .env 文件加载配置，未设置时使用默认值。
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 同时支持仓库根目录 .env 与 visual_web_agent/.env，优先读取根目录。
_env_candidates = [
    Path(__file__).resolve().parents[1] / ".env",
    Path(__file__).parent / ".env",
]
for _env_path in _env_candidates:
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=False)

# ========== VLM 模型配置 ==========
VLM_API_BASE = os.getenv("VLM_API_BASE", "http://localhost:8000/v1")
VLM_API_KEY = os.getenv("VLM_API_KEY", "EMPTY")
VLM_MODEL_NAME = os.getenv("VLM_MODEL_NAME", "qwen-vl-max")
VLM_SEMANTIC_MODEL_NAME = os.getenv("VLM_SEMANTIC_MODEL_NAME", "").strip()
VLM_TIMEOUT = int(os.getenv("VLM_TIMEOUT", "120"))
VLM_MAX_TOKENS = int(os.getenv("VLM_MAX_TOKENS", "4096"))
VLM_TEMPERATURE = float(os.getenv("VLM_TEMPERATURE", "0.1"))
VLM_TEXT_ONLY = os.getenv("VLM_TEXT_ONLY", "false").lower() == "true"
VLM_HISTORY_WINDOW = int(os.getenv("VLM_HISTORY_WINDOW", "6"))

# ========== Agent 运行配置 ==========
MAX_STEPS = int(os.getenv("MAX_STEPS", "20"))
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "./screenshots")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "800"))

# ========== 浏览器持久化配置 ==========
# 设置后会使用 launch_persistent_context 复用登录态/Cookie
# 默认放在项目目录下的 browser_data，避免把浏览器配置写进源码目录
BROWSER_USER_DATA_DIR = os.getenv(
    "BROWSER_USER_DATA_DIR",
    str(Path(__file__).parent / "browser_data"),
)
AUTH_PROFILES = os.getenv("VSPIDER_AUTH_PROFILES", "").strip()
AUTH_DIR = os.getenv(
    "VSPIDER_AUTH_DIR",
    str(Path(__file__).resolve().parents[1] / ".auth"),
)

# ========== 页面稳定等待配置 ==========
# SPA 页面加载等待超时（毫秒），用于 networkidle 检测
PAGE_STABLE_TIMEOUT = int(os.getenv("PAGE_STABLE_TIMEOUT", "15000"))
# networkidle 超时后的最低硬等待时间（秒）
PAGE_FALLBACK_WAIT = float(os.getenv("PAGE_FALLBACK_WAIT", "3.0"))

# ========== SoM 脚本路径 ==========
SOM_SCRIPT_PATH = Path(__file__).parent / "som_inject_v6.js"
