"""prestart.py (runs before the API in docker-compose.yml) and the Alembic baseline."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parent.parent
TABLES = ["detections", "fish_analyses", "users"]


def run_python(code: str, db_url: str):
    env = {**os.environ, "DATABASE_URL_INT": db_url, "ML_SERVER_URL": "http://ml.test:8000"}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)


def test_prestart_creates_the_tables_in_a_fresh_process(tmp_path):
    # Fresh interpreter: prestart must import the models itself, otherwise create_all creates nothing.
    code = "import prestart, sqlalchemy as sa; prestart.main(); print(sorted(sa.inspect(prestart.engine).get_table_names()))"
    result = run_python(code, f"sqlite:///{tmp_path}/fresh.db")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == str(TABLES)


def test_prestart_adds_columns_missing_from_an_old_fish_analyses_table(tmp_path):
    url = f"sqlite:///{tmp_path}/old.db"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, ip_address VARCHAR(45) NOT NULL)"))
        conn.execute(sa.text("CREATE TABLE fish_analyses (id INTEGER PRIMARY KEY, user_id INTEGER)"))

    result = run_python("import prestart; prestart.check_and_create_tables()", url)

    assert result.returncode == 0, result.stderr
    columns = {c["name"] for c in sa.inspect(engine).get_columns("fish_analyses")}
    assert {"image_path", "processed_image_path", "total_objects"} <= columns


def test_alembic_baseline_matches_the_models(tmp_path):
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path / "pg", cleanup_mode="stop")
    env = {**os.environ, "DATABASE_URL_INT": server.get_uri(), "ML_SERVER_URL": "http://ml.test:8000"}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)

    heads = alembic("heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.split()[0] == "0001"  # a single revision

    upgrade = alembic("upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr
    tables = sa.inspect(sa.create_engine(server.get_uri())).get_table_names()
    assert sorted(t for t in tables if t != "alembic_version") == TABLES

    check = alembic("check")  # no difference between the migrated schema and the models
    assert check.returncode == 0, check.stdout + check.stderr
