import asyncio
import json
from types import SimpleNamespace

import aio_pika

from rabbitmq.rabbitmq_client import AsyncRabbitMQClient


class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((message, routing_key))


class FakeChannel:
    def __init__(self):
        self.default_exchange = FakeExchange()
        self.declared = []

    async def declare_queue(self, name, **kwargs):
        self.declared.append((name, kwargs))


def connected_client(channel):
    client = AsyncRabbitMQClient()
    client.connected = True
    client.connection = SimpleNamespace(is_closed=False)
    client.channel = channel
    return client


TASK = {
    "image_bytes": "ZmFrZQ==",
    "image_path": "fish.jpg",
    "filename": "fish.jpg",
    "content_type": "image/jpeg",
    "image_hash": "abc",
    "user_id": 1,
}


def test_send_publishes_one_persistent_message_and_declares_no_reply_queue():
    channel = FakeChannel()
    client = connected_client(channel)

    task_id = asyncio.run(client.send(TASK, timeout=60))

    assert channel.declared == []  # no per-task queue: the result travels through Redis
    (message, routing_key), = channel.default_exchange.published
    assert routing_key == "fish_analysis_queue"
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert message.correlation_id == task_id
    assert message.reply_to is None
    body = json.loads(message.body)
    assert (body["task_id"], body["result_queue"], body["timeout"]) == (task_id, "", 60)
    assert body["image_bytes"] == "ZmFrZQ==" and body["image_path"] == "fish.jpg"


def test_send_returns_none_when_publishing_fails():
    channel = FakeChannel()
    client = connected_client(channel)

    async def broken_publish(message, routing_key):
        raise RuntimeError("channel closed")

    channel.default_exchange.publish = broken_publish

    assert asyncio.run(client.send(TASK)) is None


def test_send_returns_none_for_an_invalid_task():
    client = connected_client(FakeChannel())

    assert asyncio.run(client.send({"filename": "only-this"})) is None
