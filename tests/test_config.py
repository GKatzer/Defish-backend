"""Configuration is read from the environment at import time, so each case runs in a fresh interpreter."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGED = ("ML_SERVER_URL", "REDIS_URL", "CACHE_SAVING_TIME", "MAX_UPLOAD_MB", "DATABASE_URL", "DATABASE_URL_INT")


def run_python(code: str, **env):
    full_env = {k: v for k, v in os.environ.items() if k not in MANAGED}
    full_env.update(env)
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full_env, capture_output=True, text=True)


def value_of(expression: str, module: str, **env) -> str:
    result = run_python(f"import {module}; print({expression})", **env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_ml_server_url_without_scheme_gets_http():
    assert value_of("config.ML_SERVER_URL", "config", ML_SERVER_URL="ml.local:9000") == "http://ml.local:9000"


def test_ml_server_url_keeps_https_and_drops_the_trailing_slash():
    assert value_of("config.ML_SERVER_URL", "config", ML_SERVER_URL="https://ml.local/") == "https://ml.local"


def test_missing_ml_server_url_stops_the_import():
    result = run_python("import config")
    assert result.returncode != 0
    assert "ML_SERVER_URL is not set" in result.stderr


def test_cache_lifetime_defaults_to_an_hour_and_can_be_changed():
    assert value_of("config.CACHE_SAVING_TIME", "config", ML_SERVER_URL="x:1") == "3600"
    assert value_of("config.CACHE_SAVING_TIME", "config", ML_SERVER_URL="x:1", CACHE_SAVING_TIME="90") == "90"


def test_upload_limit_defaults_to_ten_megabytes_and_can_be_changed():
    assert value_of("config.MAX_UPLOAD_BYTES", "config", ML_SERVER_URL="x:1") == str(10 * 1024 * 1024)
    assert value_of("config.MAX_UPLOAD_BYTES", "config", ML_SERVER_URL="x:1", MAX_UPLOAD_MB="0.5") == str(512 * 1024)


def test_redis_url_is_optional():
    assert value_of("config.REDIS_URL", "config", ML_SERVER_URL="x:1") == "None"
    assert value_of("config.REDIS_URL", "config", ML_SERVER_URL="x:1", REDIS_URL="redis://r:6379") == "redis://r:6379"


def test_database_url_int_wins_over_database_url():
    both = {"DATABASE_URL_INT": "sqlite:///int.db", "DATABASE_URL": "sqlite:///other.db"}
    assert value_of("database.DATABASE_URL", "database", **both) == "sqlite:///int.db"


def test_database_url_is_the_fallback():
    assert value_of("database.DATABASE_URL", "database", DATABASE_URL="sqlite:///other.db") == "sqlite:///other.db"


def test_database_default_points_at_the_compose_service():
    default = value_of("database.DATABASE_URL", "database")
    assert default.endswith("@postgres:5432/fishguard")
