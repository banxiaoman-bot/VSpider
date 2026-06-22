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
VLM_SEMANTIC_API_BASE = os.getenv("VLM_SEMANTIC_API_BASE", "").strip()
VLM_SEMANTIC_API_KEY = os.getenv("VLM_SEMANTIC_API_KEY", "").strip()
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

# ========== 网络代理（降低 CF / 反爬触发） ==========
PROXY_SERVER = os.getenv("VSPIDER_PROXY_SERVER", "").strip()
PROXY_USERNAME = os.getenv("VSPIDER_PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.getenv("VSPIDER_PROXY_PASSWORD", "").strip()
# 代理链（多代理轮换，遇阻即换路）。逗号分隔，每项支持 host:port /
# scheme://host:port / user:pass@host:port；为空则回退到上面的单一 PROXY_SERVER。
PROXY_CHAIN = [p.strip() for p in os.getenv("VSPIDER_PROXY_CHAIN", "").split(",") if p.strip()]
PROXY_STRATEGY = os.getenv("VSPIDER_PROXY_STRATEGY", "round_robin").strip().lower()

# ========== 可选第三方 CAPTCHA Solver ==========
CAPTCHA_SOLVER = os.getenv("VSPIDER_CAPTCHA_SOLVER", "capsolver").strip().lower()
CAPTCHA_API_KEY = os.getenv("VSPIDER_CAPTCHA_API_KEY", "").strip()


def apply_run_constraints(raw: dict | None) -> None:
    """Apply per-run ``input_contract.constraints`` onto module-level config."""
    if not raw:
        return
    global PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD, PROXY_CHAIN, PROXY_STRATEGY
    proxy_server = str(raw.get("proxy_server") or raw.get("proxy") or "").strip()
    if proxy_server:
        PROXY_SERVER = proxy_server
    proxy_user = str(raw.get("proxy_username") or raw.get("proxy_user") or "").strip()
    if proxy_user:
        PROXY_USERNAME = proxy_user
    proxy_pass = str(raw.get("proxy_password") or raw.get("proxy_pass") or "").strip()
    if proxy_pass:
        PROXY_PASSWORD = proxy_pass
    proxy_chain = raw.get("proxy_chain")
    if proxy_chain:
        if isinstance(proxy_chain, str):
            PROXY_CHAIN = [p.strip() for p in proxy_chain.split(",") if p.strip()]
        elif isinstance(proxy_chain, (list, tuple)):
            PROXY_CHAIN = [str(p).strip() for p in proxy_chain if str(p).strip()]
    proxy_strategy = str(raw.get("proxy_strategy") or "").strip().lower()
    if proxy_strategy:
        PROXY_STRATEGY = proxy_strategy

# ========== 页面稳定等待配置 ==========
# SPA 页面加载等待超时（毫秒），用于 networkidle 检测
PAGE_STABLE_TIMEOUT = int(os.getenv("PAGE_STABLE_TIMEOUT", "15000"))
# networkidle 超时后的最低硬等待时间（秒）
PAGE_FALLBACK_WAIT = float(os.getenv("PAGE_FALLBACK_WAIT", "3.0"))

# ========== RPA 肌肉记忆配置 ==========
# 物理回放：历史 click/type/goto/xpath 轨迹的极速回放。
RPA_ENABLE_PHYSICAL_REPLAY = (
    os.getenv("VSPIDER_RPA_ENABLE_PHYSICAL_REPLAY", "true").lower()
    in {"1", "true", "yes", "on"}
)
# 语义宏回放：date_pick / cascader_pick 这类确定性宏动作。
RPA_ENABLE_SEMANTIC_MACRO_REPLAY = (
    os.getenv("VSPIDER_RPA_ENABLE_SEMANTIC_MACRO_REPLAY", "true").lower()
    in {"1", "true", "yes", "on"}
)

# ========== Date picker RPA 配置 ==========
# 默认 false：JS date_pick 失败后交给 VLM 视觉兜底。
# true：允许 JS 兜底直接写入 input value 并触发 input/change/blur 事件。
DATE_PICK_DIRECT_SET_FALLBACK = (
    os.getenv("VSPIDER_DATE_PICK_DIRECT_SET_FALLBACK", "false").lower()
    in {"1", "true", "yes", "on"}
)

# ========== Message Compaction 配置 ==========
# 历史记录超过此条数时触发 LLM 摘要压缩
COMPACTION_TRIGGER_COUNT = int(os.getenv("VSPIDER_COMPACTION_TRIGGER_COUNT", "12"))
# 历史文本超过此字符数时也触发压缩
COMPACTION_TRIGGER_CHARS = int(os.getenv("VSPIDER_COMPACTION_TRIGGER_CHARS", "8000"))
# 压缩后保留的最近原始记录数
COMPACTION_KEEP_LAST = int(os.getenv("VSPIDER_COMPACTION_KEEP_LAST", "6"))
# 两次压缩之间的最小步数间隔
COMPACTION_COOLDOWN = int(os.getenv("VSPIDER_COMPACTION_COOLDOWN", "5"))
# 是否启用 Message Compaction
COMPACTION_ENABLED = os.getenv("VSPIDER_COMPACTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

# ========== Judge（任务完成验证）配置 ==========
JUDGE_ENABLED = os.getenv("VSPIDER_JUDGE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

# ========== A11y Enhancer（无障碍树增强）配置 ==========
A11Y_ENHANCER_ENABLED = os.getenv("VSPIDER_A11Y_ENHANCER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

# ========== DC-2 Consent Guard（感知前置自动同意墙关闭）配置 ==========
# 在 perception 入口对每个新 URL 自动调一次 dismiss_consent（幂等、按 URL 去重）。
# 设 VSPIDER_CONSENT_GUARD_ENABLED=0 可关闭，回退为仅显式 dismiss_consent 动作。
CONSENT_GUARD_ENABLED = os.getenv("VSPIDER_CONSENT_GUARD_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

# ========== Response Cache（响应缓存 / Replay 模式）配置 ==========
# off / record / replay
CACHE_MODE = os.getenv("VSPIDER_CACHE_MODE", "off").strip().lower()
# 缓存目录
CACHE_DIR = os.getenv("VSPIDER_CACHE_DIR", ".cache/vlm_responses")
# Session ID（留空时自动用时间戳生成）
CACHE_SESSION_ID = os.getenv("VSPIDER_CACHE_SESSION_ID", "").strip()
# Replay 模式下输入哈希不一致时是否抛错（默认仅警告）
CACHE_REPLAY_STRICT = os.getenv("VSPIDER_CACHE_REPLAY_STRICT", "false").lower() in {"1", "true", "yes", "on"}
# Replay 模式找不到记录时是否回落到真实调用（默认抛错）
CACHE_REPLAY_FALLBACK = os.getenv("VSPIDER_CACHE_REPLAY_FALLBACK", "false").lower() in {"1", "true", "yes", "on"}

# ========== Element Tracker（自适应元素重定位）配置 ==========
# 是否启用元素追踪器（VLMClient 上的 _element_tracker 实例）。
# 启用后 main.py / guard 可按需调用 vlm.track_element() / vlm.relocate_element()
# 在 DOM 重排后按结构签名找回元素。OFF 时所有方法仍可调用但是 no-op。
ELEMENT_TRACKER_ENABLED = os.getenv("VSPIDER_ELEMENT_TRACKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
# 匹配阈值（0.0~1.0）；超过此值视为成功重定位
ELEMENT_TRACKER_MATCH_THRESHOLD = float(os.getenv("VSPIDER_ELEMENT_TRACKER_MATCH_THRESHOLD", "0.55"))
# 高置信度门槛
ELEMENT_TRACKER_HIGH_CONFIDENCE = float(os.getenv("VSPIDER_ELEMENT_TRACKER_HIGH_CONFIDENCE", "0.80"))

# ========== SoM 脚本路径 ==========
SOM_SCRIPT_PATH = Path(__file__).parent / "som_inject_v6.js"
