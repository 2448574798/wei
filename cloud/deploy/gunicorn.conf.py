import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("WEI_LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

bind = f"{os.getenv('APP_HOST', '127.0.0.1')}:{os.getenv('APP_PORT', '8000')}"
# Browser Worker websocket connections are held in process memory. Keep one
# worker unless the hub is moved to a shared external broker.
requested_workers = int(os.getenv("GUNICORN_WORKERS", "1"))
browser_worker_enabled = os.getenv("BROWSER_WORKER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
workers = 1 if browser_worker_enabled else requested_workers
worker_class = "uvicorn.workers.UvicornWorker"
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))
accesslog = str(LOG_DIR / "wei_agent_access.log")
errorlog = str(LOG_DIR / "wei_agent_error.log")
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
proc_name = "wei_agent"
