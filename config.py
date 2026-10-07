# config.py
"""Shared configuration, read from the environment."""
import os


def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


_raw_ml_server_url = os.getenv("ML_SERVER_URL")
if not _raw_ml_server_url:
    raise RuntimeError("ML_SERVER_URL is not set (host:port of the ML server, no path)")

# Base address of the ML server (host:port), no path.
# main.py and worker.py add the path they need (/health, /analyze) themselves.
ML_SERVER_URL = _normalize_url(_raw_ml_server_url)

# Redis address (redis://host:port). If unset, utils.cache tries localhost, redis, 127.0.0.1,
# and the worker uses the host "redis" (the service name in docker-compose).
REDIS_URL = os.getenv("REDIS_URL")

# Lifetime (seconds) of the result cache keyed by the image hash, keys "fish:*".
CACHE_SAVING_TIME = int(float(os.getenv("CACHE_SAVING_TIME", 3600)))

# Maximum size of an uploaded photo, MB. Larger gets a 413: the API reads the whole file into memory.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", 10))
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)

# Lifetime (seconds) of the keys of one task: result:*, error:*, cancel:*, task_metadata:*.
TASK_KEY_TTL = 3600
