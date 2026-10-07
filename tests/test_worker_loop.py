"""The consumer loop of worker.py with a fake pika connection (no broker needed)."""
import json

import pika
import pytest

import worker
from rabbitmq.schemas import RabbitMQTask


class FakeChannel:
    def __init__(self, session):
        self.session = session
        self.declared = []
        self.qos = None
        self.consuming_queue = None
        self.stopped = False

    def queue_declare(self, queue, durable):
        self.declared.append((queue, durable))

    def basic_qos(self, prefetch_count):
        self.qos = prefetch_count

    def basic_consume(self, queue, on_message_callback, auto_ack):
        self.consuming_queue = (queue, auto_ack)

    def start_consuming(self):
        self.session.run_consuming(self)

    def stop_consuming(self):
        self.stopped = True


class FakeConnection:
    def __init__(self, session):
        self.session = session
        self.channel_obj = FakeChannel(session)
        self.is_open = True
        self.is_closed = False

    def channel(self):
        return self.channel_obj

    def add_callback_threadsafe(self, callback):
        callback()

    def close(self):
        self.is_open, self.is_closed = False, True


class Session:
    """Scripted broker behaviour: what every successive start_consuming() does."""

    def __init__(self, worker_obj, actions):
        self.worker = worker_obj
        self.actions = list(actions)
        self.connections = []

    def connect(self):
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def run_consuming(self, channel):
        action = self.actions.pop(0)
        if action == "stop":
            self.worker.should_stop = True  # as the signal handler would
            return
        raise action


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.time, "sleep", calls.append)
    return calls


@pytest.fixture
def handlers(monkeypatch):
    installed = {}
    monkeypatch.setattr(worker.signal, "signal", lambda sig, handler: installed.update({sig: handler}))
    return installed


def test_worker_reconnects_after_the_broker_drops_the_connection(monkeypatch, sleeps, handlers):
    w = worker.Worker()
    session = Session(w, [
        pika.exceptions.ConnectionClosedByBroker(320, "CONNECTION_FORCED"),
        pika.exceptions.AMQPConnectionError("lost"),
        "stop",
    ])
    monkeypatch.setattr(w, "_connect", session.connect)

    w.start()

    assert len(session.connections) == 3  # it used to exit after the first drop
    assert sleeps == [5, 10]
    assert all(c.is_closed for c in session.connections)
    for connection in session.connections:
        assert connection.channel_obj.declared == [("fish_analysis_queue", True)]
        assert connection.channel_obj.qos == 1
        assert connection.channel_obj.consuming_queue == ("fish_analysis_queue", False)


def test_worker_keeps_retrying_while_the_broker_is_unreachable(monkeypatch, sleeps, handlers):
    w = worker.Worker()
    session = Session(w, ["stop"])
    attempts = []

    def connect():
        attempts.append(1)
        return None if len(attempts) <= 2 else session.connect()

    monkeypatch.setattr(w, "_connect", connect)

    w.start()

    assert sleeps == [10, 10]
    assert len(session.connections) == 1


def test_sigterm_asks_the_running_consumer_to_stop_after_the_current_task(sleeps, handlers):
    w = worker.Worker()
    connection = FakeConnection(Session(w, []))

    w.should_stop = True  # start() returns at once, but only after it has installed the signal handlers
    w.start()
    w.should_stop = False
    w._connection, w._channel = connection, connection.channel_obj  # a consumer is running

    handlers[worker.signal.SIGTERM](worker.signal.SIGTERM, None)

    assert w.should_stop is True
    assert connection.channel_obj.stopped is True


def test_signal_during_connection_setup_does_not_block_in_start_consuming(monkeypatch):
    w = worker.Worker()
    session = Session(w, [])
    connection = session.connect()
    w.should_stop = True

    w._consume(connection)  # would raise IndexError if it entered start_consuming()

    assert session.actions == []


class FakeMethod:
    delivery_tag = 7


class FakeCh:
    def __init__(self):
        self.acked, self.nacked, self.published = [], [], []

    def basic_ack(self, delivery_tag):
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue):
        self.nacked.append((delivery_tag, requeue))

    def basic_publish(self, **kwargs):
        self.published.append(kwargs)


def task_body(**extra):
    task = RabbitMQTask(task_id="t", image_path="a.jpg", filename="a.jpg", image_bytes="ZmFrZQ==",
                        image_hash="h", user_id=1, **extra)
    return json.dumps(task.model_dump()).encode()


def test_a_failed_analysis_is_acknowledged_and_not_retried(monkeypatch):
    w = worker.Worker()
    result = worker.TaskResult(task_id="t", success=False, error="ML service unavailable")
    monkeypatch.setattr(w, "process_task", lambda task: result)
    ch = FakeCh()

    w._on_message(ch, FakeMethod(), None, task_body())

    assert ch.acked == [7] and ch.nacked == []
    assert w.stats["failed"] == 1 and w.stats["processed"] == 0


def test_a_malformed_message_is_dropped_without_requeue(monkeypatch):
    w = worker.Worker()
    ch = FakeCh()

    w._on_message(ch, FakeMethod(), None, b"{not json")
    w._on_message(ch, FakeMethod(), None, json.dumps({"task_id": "t"}).encode())  # fails validation

    assert ch.acked == []
    assert ch.nacked == [(7, False), (7, False)]


def test_a_task_from_an_older_api_still_gets_its_reply(monkeypatch):
    w = worker.Worker()
    result = worker.TaskResult(task_id="t", success=True, result={"id": "1"})
    monkeypatch.setattr(w, "process_task", lambda task: result)
    ch = FakeCh()

    w._on_message(ch, FakeMethod(), None, task_body(result_queue="result_t"))

    assert ch.published[0]["routing_key"] == "result_t"
    assert ch.acked == [7]
    assert w.stats["processed"] == 1
