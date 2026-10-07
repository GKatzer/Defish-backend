import logging

import redis

from config import REDIS_URL

logger = logging.getLogger(__name__)

# Hosts tried if REDIS_URL is not set (local run without compose).
_FALLBACK_HOSTS = ("localhost", "redis", "127.0.0.1")


def make_redis(**kwargs) -> redis.Redis:
    """Redis client: from REDIS_URL, otherwise the host "redis" (the service name in docker-compose)."""
    if REDIS_URL:
        return redis.Redis.from_url(REDIS_URL, **kwargs)
    return redis.Redis(host="redis", port=6379, **kwargs)


class RedisCache:
    """Thin wrapper over Redis: if Redis is unavailable, the cache is silently disabled."""

    def __init__(self):
        self.client = None
        self._init_redis()

    def _init_redis(self):
        """Try connect to Redis"""
        if REDIS_URL:
            try:
                client = make_redis(socket_timeout=2)
                client.ping()
                self.client = client
                logger.info("Redis connected (REDIS_URL)")
            except Exception as e:
                logger.warning(f"Redis not available at REDIS_URL, work without cache: {e}")
            return

        for host in _FALLBACK_HOSTS:
            try:
                client = redis.Redis(host=host, port=6379, socket_timeout=2)
                client.ping()
                self.client = client
                logger.info(f"Redis connected to {host}:6379")
                return
            except Exception:
                continue

        logger.warning("Redis not found, work without cache")

    def get(self, key):
        if not self.client:
            return None
        try:
            return self.client.get(key)
        except Exception:
            return None

    def setex(self, key, ttl, value):
        if not self.client:
            return
        try:
            self.client.setex(key, ttl, value)
        except Exception:
            pass
