# Deployment

How the stack is started, every setting, how to operate it, and what has and has not been verified.

Contents: [Requirements](#requirements) · [Services](#services) · [Startup sequence](#startup-sequence) · [Configuration](#configuration) · [Running it](#running-it) ·
[Operating it](#operating-it) · [Database schema and Alembic](#database-schema-and-alembic) · [Upgrading](#upgrading-from-the-previous-version-of-this-repository) · [Hardening checklist](#hardening-checklist) · [What was verified](#what-was-verified)

## Requirements

- Docker with Compose v2. Verified with Docker 29.8.1 and Compose 5.5.1 on Linux x86-64.
- An inference service reachable from the containers (`ML_SERVER_URL`), or the mock in [`examples/`](examples/), which needs nothing else.
- About 1.9 GB of memory if every container reached its limit (200 + 3 x 300 + 300 + 200 + 300 MB); at idle the whole stack used about 0.5 GB (below).

## Services

| Service | Port | Published | Restart policy |
|---|---|---|---|
| `fastapi` | 8000 in the container | `127.0.0.1:8001` on the host | `unless-stopped` |
| `worker` x3 | none | no | `unless-stopped` |
| `rabbitmq` (AMQP 5672, management UI 15672) | internal | no | `always` |
| `redis` | 6379, internal | no | none |
| `postgres` | 5432, internal | no | none |

Everything shares one bridge network, `shared-network`. The only published port is bound to the loopback interface; to reach the API from another machine put a reverse proxy in front of it (none is part of this repository).
Memory and CPU limits per service are in [architecture.md](architecture.md#components).

## Startup sequence

1. `postgres`, `redis` and `rabbitmq` start with health checks (`pg_isready`, `redis-cli ping`, `rabbitmq-diagnostics -q check_port_connectivity`: the broker counts as healthy only when its AMQP port accepts connections, see [decision 15](design-decisions.md#15-the-brokers-health-check-looks-at-the-amqp-port)).
2. `fastapi` starts when all three are healthy and runs `python prestart.py`: waits for the database (30 attempts, 2 s apart), creates the three tables with `create_all`, and, if `fish_analyses` already exists, adds the columns `image_path`, `processed_image_path`, `total_objects` when missing.
3. Then `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2`.
4. The `worker` replicas start when `rabbitmq` and `redis` are healthy; they do not wait for the API or PostgreSQL (a task can only exist after the API has accepted it, and by then the tables exist).

The container's default command (`Dockerfile`) is different: it runs `prestart.py` and uvicorn with `logging.yaml`, writing to `logs/fastapi.log`; docker-compose overrides it, so the log goes to the container's output (`docker compose logs`).
The image creates `logs/` (verified).

## Configuration

All settings are environment variables. docker-compose reads `.env` for substitution **and** passes the whole file to every container (`env_file`), including the database password to the API and workers and `ML_SERVER_URL` to PostgreSQL; keep `.env` out of git (the `.gitignore` of the repository does).
[`env-example.txt`](../env-example.txt) is the template. Checked against the code with `grep -rn getenv` and against the rendered compose file.

| Variable | Read by | Meaning | Default | Required |
|---|---|---|---|---|
| `ML_SERVER_URL` | API, worker | base address of the inference service, `host:port` or `http://host:port`; `http://` is added when missing and a trailing `/` removed; routes `/health` and `/analyze` are appended | none: the process stops at start | yes |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | postgres container; docker-compose (builds the database URL) | database user, password (URL-safe characters only, it ends up in a URL) and name | none | yes with the compose file |
| `DATABASE_URL_INT` | API, worker, Alembic | SQLAlchemy URL of the database; wins over `DATABASE_URL`; docker-compose builds it from `POSTGRES_*` when it is not set | built by compose; outside compose `postgresql://fishuser:fishpass@postgres:5432/fishguard` | no |
| `DATABASE_URL` | API, worker, Alembic | fallback when `DATABASE_URL_INT` is empty | none | no |
| `REDIS_URL` | API, worker | `redis://host:port`; without it the API tries `localhost`, `redis`, `127.0.0.1` on port 6379 and the worker uses host `redis` | `redis://redis:6379` set by compose | no |
| `CACHE_SAVING_TIME` | worker | lifetime in seconds of a cached analysis (`fish:*`); other task keys always live 3600 s | `3600` | no |
| `MAX_UPLOAD_MB` | API | largest accepted upload in MB (read completely into the API's memory); larger files get `413` | `10` | no |
| `RABBITMQ_HOST`, `RABBITMQ_PORT` | API, worker | broker address | `localhost`, `5672`; compose sets `rabbitmq`, `5672` | no |
| `RABBITMQ_USERNAME`, `RABBITMQ_PASSWORD` | API, worker, rabbitmq container (as `RABBITMQ_DEFAULT_USER/PASS`) | broker credentials | `guest` / `guest` | no, but change them |

Not configurable (constants in the code): queue name `fish_analysis_queue`, 300 s call timeout (`ML_ANALYZE_TIMEOUT`), 3600 s lifetime of task keys (`TASK_KEY_TTL`), prefetch of one task, Redis port 6379.
Mock inference service only (the compose override passes the last two through from the shell): `MOCK_HOST` (`127.0.0.1`), `MOCK_PORT` (`8000`), `MOCK_DELAY_SECONDS` (`8`, for file names containing `slow`), `MOCK_USE_LAYOUT` (`0`; `1` puts the boxes on the four fish of `docs/examples/aquarium-synthetic.jpg`, which the web client's screenshots use).

## Running it

**With the mock inference service** (nothing else needed; verified):

```bash
cat > .env <<'EOF'
ML_SERVER_URL=http://ml:8000
POSTGRES_USER=fishuser
POSTGRES_PASSWORD=CHANGE_ME
POSTGRES_DB=fishguard
EOF
docker compose -f docker-compose.yml -f docs/examples/docker-compose.mock-ml.yml up -d --build
curl -s http://127.0.0.1:8001/handshake
```

The override adds a service `ml` that runs `docs/examples/mock_ml_server.py` from the same image; `http://ml:8000` is its address on the compose network.

**With the real inference service** (not run for this documentation: no model weights were available): set `ML_SERVER_URL` to its address (for example `http://<ml-host>:8000`) and start with `docker compose up -d --build`.
The service must be reachable from the containers, not only from the host.

## Operating it

```bash
docker compose ps                                    # state of all services
docker compose logs -f worker                        # worker log
docker compose up -d --scale worker=5                # more concurrent analyses; each worker takes one task at a time
docker compose stop worker                           # SIGTERM: running tasks are finished first (up to Docker's 10 s grace period; add -t N for longer)
docker compose exec rabbitmq rabbitmqctl list_queues name messages consumers   # one queue, fish_analysis_queue
docker compose exec redis redis-cli info stats | grep evicted_keys             # non-zero: results were evicted
docker compose down -v                               # stop and delete all data (volumes)
```

A healthy stack shows one queue (`fish_analysis_queue`) with 3 consumers and `0` messages when idle (*measured*, see [stack-checks.txt](examples/transcripts/stack-checks.txt)). Anything named `result_<uuid>` means an old version of the code is still running.

At idle the containers used (`docker stats`, *measured*): `fastapi` 154 MiB of 200, `rabbitmq` 125 of 300, `worker` 54 of 300 each, `postgres` 36 of 300, `redis` 25 of 200. The API container is the one close to its limit.

## Database schema and Alembic

Tables come from the models: `prestart.py` runs `create_all` before the API starts. Existing tables are never altered except for the three columns mentioned above.

Alembic is configured (`alembic.ini`, `alembic/env.py` takes the connection URL from `DATABASE_URL_INT`/`DATABASE_URL`) and holds one baseline revision, `0001`, that creates the three tables. **It is not run at start.** For a database created by `prestart.py` the baseline has to be recorded once, so that future migrations know where to start:

```bash
docker compose exec fastapi alembic stamp head       # existing database: mark it as being at 0001
docker compose exec fastapi alembic check            # "No new upgrade operations detected." when models and database agree
```

Both were run against the live database of the stack (see [stack-checks.txt](examples/transcripts/stack-checks.txt)). On an empty database `alembic upgrade head` creates the same schema (checked by a test on a real PostgreSQL, `tests/test_startup.py`).
A later change of a model would be `alembic revision --autogenerate -m "..."` followed by `alembic upgrade head`; the compose file does not do this for you.

## Upgrading from the previous version of this repository

By reading the code and the compose file; no upgrade of a running deployment was exercised.

- Task messages are compatible both ways: the API now sends an empty `result_queue`, which an old worker accepts and ignores, and a new worker still answers into a `result_queue` when an old API sets one. Deploy order does not matter.
- `DATABASE_URL_INT` from `.env` keeps priority. If it is absent the compose file now builds the URL from `POSTGRES_*` instead of the old fixed `fishuser:fishpass`: make sure `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` in `.env` are the credentials the database was created with.
- `REDIS_URL` and `CACHE_SAVING_TIME` are now honoured. A leftover value in an old `.env` takes effect.
- The first Alembic revision (`ba21a226a5cc`) is gone. It could not complete on any database, so no deployment should carry its version number; if `alembic_version` holds it, `alembic stamp --purge head` replaces it with `0001` (not exercised).
- Photos in `fish_analyses.processed_image_path` written by the old code stay in the table; the new code leaves the column empty. They can be cleared with `UPDATE fish_analyses SET processed_image_path = NULL`.

## Hardening checklist

The reference deployment is a demo on a private network. Before exposing it further:

- replace the RabbitMQ defaults (`guest`/`guest`) and set a strong `POSTGRES_PASSWORD`;
- put a reverse proxy in front that sets `X-Forwarded-For`, restricts CORS to the web client's origin and blocks `/clear-cache` and `/cache-stats` (the API has no authentication);
- large photos: with 3 MB photos arriving 10 at a time the API container (200 MB) still loses processes to its memory limit, and a bigger Redis alone did not help (tried, 256 MB, before the photo got its own key); either raise the API limit or cap concurrent uploads before relying on it for phone photos (see [Limitations](../README.md#limitations));
- decide how long uploads' client addresses and file names are kept (nothing deletes them).

## What was verified

| | Status |
|---|---|
| `docker compose up --build` of the five services plus the mock, Docker 29.8.1 | run, 2026-10-04; start-up log without errors |
| full request cycle (upload, poll, cache hit, no fish, failed, canceled, unknown id), 20 photos and 50 photos of 3 MB, broker restart, SIGTERM during a task | run against the mock, [transcripts](examples/transcripts/) |
| `alembic stamp head` and `alembic check` on the live database; `upgrade head` on an empty one | run |
| `docker compose config` with the template `.env` | valid, with and without `DATABASE_URL_INT` |
| the 64 tests | pass in about 10 s (`pytest`, no Docker) |
| the real inference service | **not run** (no weights); its contract was read from its source |
| TLS, reverse proxy, a multi-host setup, a long-running (days) deployment, an upgrade of a live deployment | **not exercised** |
| continuous integration | none configured; no workflow has run on GitHub |
