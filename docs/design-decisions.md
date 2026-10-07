# Design decisions

Why the service is built the way it is, what it replaced, and what was measured. Numbers marked *measured* come from
[examples/transcripts/fixes-before-after.txt](examples/transcripts/fixes-before-after.txt): the same scenarios run against the code as it was
(commit `2516efb`) and against the current code, each on empty volumes, with the **mock** inference service and synthetic photos. Each figure is one run, so treat
differences of a few percent as noise; the large ones are not.

Contents: [1](#1-answer-with-a-task-id-and-let-the-client-poll) · [2](#2-a-queue-and-separate-workers-not-work-inside-the-request) ·
[3](#3-the-result-travels-through-redis-not-through-a-reply-queue) · [4](#4-the-cache-is-keyed-by-the-bytes-of-the-photo) ·
[5](#5-acknowledge-always-and-never-retry) · [6](#6-cancellation-is-a-flag-checked-three-times) ·
[7](#7-the-photo-is-not-written-to-disk-or-the-database) · [8](#8-one-transaction-per-analysis) ·
[9](#9-the-client-sees-short-error-texts-not-exceptions) · [10](#10-the-worker-reconnects-and-stops-gracefully) ·
[11](#11-tables-are-created-before-the-api-starts-alembic-holds-a-baseline) · [12](#12-clients-are-identified-by-address) ·
[13](#13-configuration-comes-from-the-environment-one-url-per-service) · [14](#14-the-photo-has-its-own-redis-key-and-uploads-have-a-size-limit) · [15](#15-the-brokers-health-check-looks-at-the-amqp-port) · [Considered and not done](#considered-and-not-done)

## 1. Answer with a task id and let the client poll

**Problem.** Detection plus classification of every fish takes seconds to tens of seconds, and the inference service allows up to 300 s. Holding an HTTP request open that long ties up a
connection per user, breaks behind proxies with shorter timeouts, and loses the work when the client disconnects.

**Decision.** `POST /analyze` returns a task id at once; the client polls `GET /analyze-result/{id}` (the reference web client every second). The answer has four shapes:
`processing`, `failed`, `canceled`, or the result itself.

**Alternatives.** A long-lived request (rejected, above). Server-sent events or WebSockets would avoid polling but need connection-aware proxying; polling works with plain `curl`, which also keeps
the API easy to demonstrate. The cost is idle polling traffic and a status protocol that the client has to implement completely (see the note on clients in [Limitations](../README.md#limitations)).

## 2. A queue and separate workers, not work inside the request

**Decision.** The API only validates, hashes, looks up the cache and publishes. Workers (three replicas, one task at a time each) own the call to the inference service and all writes of results. RabbitMQ carries the tasks
in a durable queue as persistent messages, so that a queued task is meant to survive a broker restart (not tested here).

**Why a broker instead of an in-process task runner.** The analysis can be slow and CPU-heavy on the inference side, while the API must stay responsive; a separate process pool can be scaled
(`deploy.replicas`, or `--scale worker=N`) and restarted independently of the API. `prefetch_count=1` makes a slow task block one worker, not a share of the queue.

**What was dropped.** `celery`, `aiohttp` and `python-dotenv` were pinned in `requirements.txt` but never imported; they were removed. The queue is used through `pika` (worker, blocking) and `aio_pika` (API, async) directly.

## 3. The result travels through Redis, not through a reply queue

**History.** The first asynchronous client published a task together with a private reply queue and had a `wait()` method that read the answer from it. In February 2026 the API was split into a `POST` and a polling `GET`
(commits of 2026-02-04 and 2026-02-15), and results moved to Redis. The reply queue was left behind: the API still declared one queue per task, the worker still
published the result into it, and nothing ever read it.

**Measured before the fix.** After 20 photos the broker held 20 queues `result_<uuid>`, each with 1 message and no consumer (690,966 bytes of message bodies, about 34.5 KB per task for a 23 KB photo, because the message carries the whole result including the photo as base64).
After 50 photos of about 3 MB, 70 such queues and 202 MiB of message bodies. In that first run RabbitMQ's own memory reading stayed at 0.14 GB and nothing was killed. Repeated runs told another story: after the first run of 50 photos of 3 MB the broker was at 0.26 GB of its 0.3 GB container limit with 26 unread queues; by the third run it had restarted (`RestartCount` 1), and the old workers, which did not reconnect, had exited; every later task stayed unprocessed ([stress-large-photos.txt](examples/transcripts/stress-large-photos.txt)). The restart cause itself was not read from the broker's log. The queues are exclusive, so the broker
deletes them when the declaring API process disconnects (standard RabbitMQ behaviour, not exercised here), which is why it never grew without bound in practice.

**Decision.** Remove the per-task queue and `wait()`. `RabbitMQTask.result_queue` stays as an optional field that is empty now, so a worker still understands a message queued by an older API.
After the change: 0 reply queues after 20 photos and after 50 photos (*measured*), and a unit test asserts that `send()` declares no queue.

## 4. The cache is keyed by the bytes of the photo

**Decision.** `fish:{md5(bytes)}` holds a finished analysis for `CACHE_SAVING_TIME` seconds (default one hour). A repeated upload is answered from Redis without a task, a queue message or a call to the models. This matters because
users re-upload the same photo while trying the demo, and each analysis costs the inference service real time.

**Details worth knowing.** The key is the content, so the file name and the user do not matter. MD5 is only a fingerprint here. A cache hit still writes an analysis row (with `total_objects = 0`),
so uploads are counted. The cached value used to include the photo (base64), which made the cache expensive in memory; it now holds only the analysis and the photo's hash (decision 14).

## 5. Acknowledge always, and never retry

**Decision.** A worker acknowledges every message it managed to parse, including those whose analysis failed. Only a message that is not valid JSON or fails validation is rejected with `nack(requeue=False)`, which drops it
(there is no dead-letter queue). Failures are reported to the client through `error:{id}`.

**Why.** A failure here is usually deterministic for the input (the inference service rejects the photo, or is down for a while). Requeueing would make a poison message spin forever on a worker with `prefetch_count=1`.
The cost is that a transient failure is not retried automatically: the client has to upload again. Unit tests pin the behaviour (`test_a_failed_analysis_is_acknowledged_and_not_retried`, `test_a_malformed_message_is_dropped_without_requeue`).

## 6. Cancellation is a flag checked three times

**Decision.** `POST /cancel/{id}` sets `cancel:{id}` in Redis. The worker looks at it before calling the inference service, after the call, and after writing the rows (just before committing). It cannot interrupt the call itself: the inference service
has no way to cancel a running request, so a canceled task still occupies its worker until the call returns (up to 300 s). The poll endpoint checks the flag first, so the client sees `canceled` immediately.

**Why a flag instead of deleting the message.** A task that a worker already holds is no longer in the queue, and a flag is the only thing both processes can see. The price is that the API cannot tell whether the id exists:
canceling an id that was never issued also answers `canceled`.

## 7. The photo is not written to disk or the database

**Before.** The API wrote every upload to `/tmp/fish_<uuid>.<ext>` in its own container, put that path in the task and the database, and expected the worker to delete it. The worker is another container and cannot see the file, so
nothing was ever deleted: after 20 photos the API container held 20 files and the workers 0 (*measured*). The same photo also travelled inside the message as base64 and was written into `fish_analyses.processed_image_path`, a `VARCHAR`
column named like a path: 614,880 characters over 20 rows, the table 720 kB against 48 kB afterwards (*measured*; a 3 MB photo would be 4 MB of text per row, arithmetic, not measured).

**Decision.** No temporary file; `image_path` is the client's file name (as it already was for cache hits); `processed_image_path` stays `NULL` because there is no processed image. The worker no longer removes anything: with a client-supplied
file name as `image_path`, an `os.remove` would let a client delete files in the worker container by naming its upload after them (a unit test keeps this closed).

## 8. One transaction per analysis

**Before.** The analysis row was committed to obtain its id, the detection rows came afterwards, and the last cancel check sat between the two. A task canceled in that window left a `fish_analyses` row announcing N objects and no detections
(1 orphan row per canceled task in a unit-level run against the old code, SQLite).

**Decision.** `flush()` instead of `commit()` for the id, one `commit()` at the end, `rollback()` on cancel. The session is closed in a `finally`, so a failure after the flush no longer leaves a connection open.

## 9. The client sees short error texts, not exceptions

**Before.** The text of the exception was stored and returned (`ML error 500`), and `/analyze` returned `Internal server error: <exception>` to the client. A connection error from `requests` includes the address of the inference service; a database error
includes details of the database.

**Decision.** The worker stores one of four texts (`ML service unavailable`, `ML service timed out`, `ML service returned HTTP <n>`, `Internal error`); the API's own errors are `Task queue error`, `couldnt get task_id`, `Internal server error`; `/handshake`
answers `ML service unreachable`. Details go to the logs. Unit tests assert that the host name of a simulated failure does not reach the client.

## 10. The worker reconnects and stops gracefully

**Before.** The code logged "reconnecting" when the broker closed the connection, but the `try` wrapped the whole `while` loop, so the first drop ended `start()`. After `docker compose restart rabbitmq` all three workers were in `Exited (0)`, and with no restart
policy in the compose file they stayed there; a task submitted afterwards was never processed (*measured*). The SIGTERM handler set a flag that a blocking `start_consuming()` never looked at, so by the code Docker's stop would have waited for its grace period and then killed the worker mid-task (not measured on the old code).

**Decision.** The `try` is inside the loop, so each failure closes the connection and the loop opens a new one (5 s after a broker-initiated close, 10 s after a connection error, 10 s while the broker is unreachable). On SIGTERM or SIGINT the handler asks the consumer to stop through
`add_callback_threadsafe`, which takes effect after the current task. `restart: unless-stopped` is set on `worker` and `fastapi` as a second line.
*Measured after the change:* all three workers logged the drop and the new session 15 s later, still `running`, and processed a task submitted afterwards; `docker compose stop` during an 8 s analysis took 6 s, exit status 0, and the result of that task was stored.

## 11. Tables are created before the API starts; Alembic holds a baseline

**Before.** `prestart.py` printed "Tables created!" but never imported the models, so `create_all` created nothing. The tables appeared when `main.py` was imported, and `main.py` is imported by both uvicorn processes at once.
On an empty database the log showed `UniqueViolation ... pg_class_relname_nsp_index` / `pg_type_typname_nsp_index` and one uvicorn process died and was restarted (*measured*, twice on two fresh stacks).

**Decision.** `prestart.py` imports the models, so `create_all` runs once, before uvicorn starts. After the change the start-up log had no error (0 matches for `Traceback` or `UniqueViolation`).

**Alembic.** The only revision had been generated by `alembic revision --autogenerate` from an `env.py` that did not import the models, so Alembic saw an empty schema and wrote a migration that **drops** `users` and `fish_analyses`. It failed on an empty database
(`DROP INDEX ix_fish_analyses_id`) and on an existing one (`DROP TABLE fish_analyses`, referenced by `detections`). It was replaced by a baseline `0001` that creates the three tables; on an empty database `alembic upgrade head` produces the schema of the models
(`alembic check`: no differences), and `alembic stamp head` marks an existing one (both *measured*). Migrations are still not run at start; see [deployment.md](deployment.md#database-schema-and-alembic).

## 12. Clients are identified by address

**Decision.** A row in `users` per client address, taken from `X-Forwarded-For` (first entry), else `X-Real-IP`, else the socket address. It is enough to count uploads per client in a demo behind a reverse proxy.

**Cost.** The header is trusted as sent: a request with `X-Forwarded-For: 203.0.113.7, 10.0.0.1` created a user `203.0.113.7` (*measured*), so any client can pick its identity. Without a proxy the stored address is the Docker network's gateway.
Addresses are personal data and are kept without a retention limit.

## 13. Configuration comes from the environment, one URL per service

**Decision.** `ML_SERVER_URL` is required and the process refuses to start without it. Redis, the database and RabbitMQ are addressed by one setting each; docker-compose builds the database URL from the same `POSTGRES_*` variables the database container reads,
unless `DATABASE_URL_INT` is set.

**Before.** `REDIS_URL` and `CACHE_SAVING_TIME` were written in the compose file and in `.env` but never read (the Redis host was hard-coded, every lifetime was 3600 s); `env-example.txt` listed `ML_SERVER_IP` while the code needs `ML_SERVER_URL`, so following
the old README produced a service that stopped at import; the compose file passed a database URL with fixed credentials that the code did not read (it read `DATABASE_URL_INT`, or fell back to the same fixed credentials).

## 14. The photo has its own Redis key and uploads have a size limit

**Before.** The worker put the photo (base64) into `result:{id}` and again into `fish:{md5}`: 4.2 MB twice for a 3 MB photo. With Redis capped at 64 MB, about eight photos filled it, and LRU evicted results that clients had not polled yet. With 10 concurrent clients that poll every second, 1 to 9 of 50 accepted uploads per run never got a result.
The API also read every upload completely, whatever its size.

**Decision.** The worker stores the photo once, as `photo:{md5}`, and the analysis without it (about 6 KB) under `result:{id}` and `fish:{md5}`, each with the photo's hash; the API fetches the photo when it answers, so the response, and therefore the web client, is unchanged.
Results of the older format, with the photo inside, are still served. The size of a result and of a cache entry is 6,113 and 6,156 bytes against 4,225,004 for a photo (*measured*). Uploads are read up to `MAX_UPLOAD_MB` plus one byte (default 10 MB) and refused with `413` beyond that, before a user row or a task is created.

**Edge cases.** If the photo has expired or been evicted, `/analyze-result` still delivers the analysis with `original_image` `null` (the client cannot draw boxes without the photo, but it keeps the diagnosis). A repeated upload whose cached analysis has lost its photo is treated as a cache miss and analysed again, which also restores the photo. Both are unit tests.

**Measured after the change** (3 MB photos, 50 per run, API 200 MB and Redis 64 MB unchanged; [stress-large-photos.txt](examples/transcripts/stress-large-photos.txt)): in the web-client-like run no accepted upload lost its result (before: 1 to 9 per run). Not solved: 10 concurrent 3 MB uploads still make processes of the API container hit the memory limit
(18, 9 and 0 of 50 uploads failed in three runs), and a client that polls only after all uploads are done still loses the results of the first tasks, because LRU evicts a 6 KB result as readily as a 4 MB photo (12 and 10 of 50 delivered).
Two cheap options were not tried: a cap on concurrent in-memory uploads, and a shorter lifetime for `photo:*` together with `volatile-ttl`, so that photos go before results.

## 15. The broker's health check looks at the AMQP port

**Before.** The compose health check of RabbitMQ was `rabbitmq-diagnostics ping`, which succeeds as soon as the Erlang node answers, before the AMQP listener on port 5672 is open (the broker needed about 34 s to finish starting on the test machine, and its log shows the plugins and listeners coming up last). `fastapi` starts when RabbitMQ is "healthy", so with the images already built the API accepted uploads
while the port was still closed: `Connect call failed (...5672)` in the log and `500 Task queue error` for the client. Found when the README's own walkthrough failed on its first upload in a clean copy; reproduced with the same result in 3 of 3 warm starts, 9 of 9 uploads in the first seconds. (The first cold builds had hidden it: building the images took longer than the broker's start.) The API recovers by itself on the next upload that finds the port open.

**Decision.** The health check is `rabbitmq-diagnostics -q check_port_connectivity`, which tries the listeners. After the change 9 of 9 first uploads of 3 warm starts were accepted.

## Considered and not done

| Idea | Why not (yet) |
|---|---|
| authentication, rate limiting, protecting `/clear-cache` and `/cache-stats` | a design decision for the deployment (the reference deployment sits behind a private network); not added to the code |
| a cap on concurrent in-memory uploads; photos evicted before results (`volatile-ttl`, shorter lifetime for `photo:*`) | the two remaining weak spots of decision 14; not tried yet |
| `404` for unknown task ids | the API would need to know which ids it issued (`task_metadata:*` is written for this but is never read) and the reference client would need to handle it |
| retries and a dead-letter queue | see decision 5; needs a rule for which failures are transient |
| checking that an upload is an image | the API accepts any file up to the size limit (a text file became a task in [error-cases.txt](examples/transcripts/error-cases.txt)); the inference service decides |
| `SCAN` instead of `KEYS` in `/cache-stats` and `/clear-cache` | `KEYS` blocks Redis while it runs; harmless at this size, wrong at scale |
| running Alembic at start | existing databases were created by `create_all`; they would first have to be stamped |
| aborting the inference call on cancel | needs support on the inference side |
| storing `uncertain` and `top3` | the response has them, the tables have no columns; a migration would be the first real use of Alembic here |
