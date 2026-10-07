"""Test environment.

The application reads its configuration from the environment when a module is imported, so the variables are set
here, before any application module is imported. Redis is replaced by an in-process fake that the API and the
worker share, the database is a throw-away SQLite file. No Docker, broker or network is needed.
"""
import os
import tempfile

os.environ["ML_SERVER_URL"] = "http://ml.test:8000"
os.environ["REDIS_URL"] = "redis://fake-redis:6379"
os.environ["CACHE_SAVING_TIME"] = "120"
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ.pop("DATABASE_URL", None)
os.environ["DATABASE_URL_INT"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="fishguard-tests-"), "test.db")

import fakeredis  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402

FAKE_REDIS_SERVER = fakeredis.FakeServer()


def _fake_from_url(cls, url, **kwargs):
    return fakeredis.FakeRedis(server=FAKE_REDIS_SERVER, **kwargs)


# utils.cache builds every client through Redis.from_url(REDIS_URL, ...)
redis.Redis.from_url = classmethod(_fake_from_url)


@pytest.fixture
def fake_redis():
    """Text-mode client on the same fake server the application uses; the server is emptied before each test."""
    client = fakeredis.FakeRedis(server=FAKE_REDIS_SERVER, decode_responses=True)
    client.flushall()
    return client


@pytest.fixture(autouse=True)
def clean_db():
    from database import Base, engine
    import models.models  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    from database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
