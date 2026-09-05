"""统一日志模块：控制台 + 文件（按天/大小轮转）。

用法：
    from logger import get_logger
    log = get_logger("app")        # 常规模块日志
    log = get_logger("generate")   # 单次生成链路可带 request_id

    log.info("..."); log.error("...", exc_info=True)

日志文件默认写入 <项目根>/logs/app.log，通过环境变量 LOG_DIR 可改。
级别默认 INFO，可用环境变量 LOG_LEVEL 覆盖（DEBUG/INFO/WARNING/ERROR）。
"""
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = Path(os.environ.get("LOG_DIR", str(BASE_DIR / "logs")))
LOG_FILE = LOG_DIR / "app.log"
LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(),
                    logging.INFO)

# 单次请求/生成链路的追踪 id（线程局部，便于把一次生成的所有日志串起来）
_local = threading.local()

# 控制台与文件共用的格式
_FILE_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(threadName)s | %(message)s"
_CONSOLE_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"

_config_lock = threading.Lock()
_configured = False


def set_request_id(rid: str = ""):
    """为当前线程设置追踪 id（如一次生成流程），空则清空。"""
    _local.request_id = rid or None


def get_request_id() -> str:
    return getattr(_local, "request_id", None) or ""


class _RequestFilter(logging.Filter):
    """给每条日志附加 request_id 字段（存在时）。"""

    def filter(self, record):
        record.request_id = get_request_id()
        return True


def setup_logging():
    """初始化根 logger：文件(轮转) + 控制台。可重复调用（幂等）。"""
    global _configured
    with _config_lock:
        if _configured:
            return
        _configured = True

        root = logging.getLogger()
        root.setLevel(LOG_LEVEL)

        # 清掉可能残留的 handler，避免重复输出
        for h in list(root.handlers):
            root.removeHandler(h)

        # ---- 文件：按 5MB 轮转，保留 5 份 ----
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5,
                encoding="utf-8")
            fh.setLevel(LOG_LEVEL)
            fh.setFormatter(logging.Formatter(_FILE_FMT))
            fh.addFilter(_RequestFilter())
            root.addHandler(fh)
        except Exception as e:
            print(f"[logger] 无法创建文件日志 {LOG_FILE}: {e}", file=sys.stderr)

        # ---- 控制台 ----
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(LOG_LEVEL)
        ch.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
        ch.addFilter(_RequestFilter())
        root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """获取带 request_id 支持的 logger。首次调用自动初始化。"""
    setup_logging()
    logger = logging.getLogger(name)
    if not any(isinstance(f, _RequestFilter) for f in logger.filters):
        logger.addFilter(_RequestFilter())
    return logger
