import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from langchain_openai import ChatOpenAI
from slowapi import Limiter
from slowapi.util import get_remote_address


SRC_DIR = Path(__file__).resolve().parent
BASE_DIR = SRC_DIR.parent
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("wei_agent")
    if logger.handlers:
        return logger

    log_dir = get_path_from_env("WEI_LOG_DIR", BASE_DIR / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_dir / "wei_agent.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def load_system_prompt() -> str:
    prompt_candidates = [
        os.getenv("SYSTEM_PROMPT_PATH"),
        str(BASE_DIR / "config" / "system_prompt.txt"),
    ]
    for candidate in prompt_candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path.read_text(encoding="utf-8")
    return DEFAULT_SYSTEM_PROMPT


load_dotenv(BASE_DIR / ".env")
logger = configure_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
ONE_API_URL = os.getenv("ONE_API_URL", "http://127.0.0.1:3000/v1").rstrip("/")
ONE_API_TOKEN = os.getenv("ONE_API_TOKEN", "").strip()
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
DISPATCHER_MODEL = os.getenv("DISPATCHER_MODEL", "gpt-4o-mini")
EXECUTION_MODEL_SIMPLE = os.getenv("EXECUTION_MODEL_SIMPLE", "gpt-4o-mini")
EXECUTION_MODEL_STANDARD = os.getenv("EXECUTION_MODEL_STANDARD", "gpt-4o")
EXECUTION_MODEL_ADVANCED = os.getenv("EXECUTION_MODEL_ADVANCED", "gpt-5.4")
LOCAL_EXECUTION_MODEL = os.getenv("LOCAL_EXECUTION_MODEL", EXECUTION_MODEL_ADVANCED)
ONLINE_RESEARCH_MODEL = os.getenv("ONLINE_RESEARCH_MODEL", "gpt-4o-mini-search-preview")
ONLINE_RESEARCH_MAX_TOKENS = int(os.getenv("ONLINE_RESEARCH_MAX_TOKENS", "420"))
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "wei_session")
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower() == "true"
SYSTEM_PROMPT_TEXT = load_system_prompt()

if not ONE_API_TOKEN:
    logger.warning("ONE_API_TOKEN is not set. Chat requests will fail until it is configured.")

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
_llm_cache: dict[tuple[str, str, float], ChatOpenAI] = {}


def get_llm(model_name: str, temperature: float = 0.2) -> ChatOpenAI:
    if not ONE_API_TOKEN:
        raise RuntimeError("ONE_API_TOKEN is not configured.")

    key = (model_name, ONE_API_URL, temperature)
    if key not in _llm_cache:
        _llm_cache[key] = ChatOpenAI(
            model=model_name,
            temperature=temperature,
            openai_api_key=ONE_API_TOKEN,
            openai_api_base=ONE_API_URL,
        )
    return _llm_cache[key]
