import base64
import glob
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import main
import worker
from models.models import FishAnalysis, User
from rabbitmq.schemas import RabbitMQTask

IMAGE = b"fake-image-bytes"
IMAGE_HASH = hashlib.md5(IMAGE).hexdigest()
UPLOAD = {"image": ("fish.jpg", IMAGE, "image/jpeg")}

RESULT = {
    "id": "7",
    "diagnosis": "fin_rot",
    "confidence": 0.88,
    "recommendations": "text",
    "original_image": "ZmFrZQ==",
    "image_format": "jpeg",
    "image_width": 640,
    "image_height": 480,
    "detections": [{"classification_class": "fin_rot", "classification_confidence": 0.88}],
}


class FakeQueue:
    """Stands in for the RabbitMQ client of the API."""

    def __init__(self):
        self.sent = []
        self.task_id = "task-123"
        self.fail = False

    async def send(self, task_data, timeout):
        if self.fail:
            raise ConnectionError("amqp://secret-broker-host unreachable")
        self.sent.append(task_data)
        return self.task_id


@pytest.fixture
def queue(monkeypatch):
    fake = FakeQueue()

    async def get_client():
        return fake

    monkeypatch.setattr(main, "get_async_rabbitmq_client", get_client)
    return fake


@pytest.fixture
def client(fake_redis):
    return TestClient(main.app)


def test_root(client):
    assert client.get("/").json() == {"message": "Defish API is running"}


def test_analyze_enqueues_a_task(client, queue, fake_redis):
    temp_files_before = set(glob.glob("/tmp/fish_*"))

    response = client.post("/analyze", files=UPLOAD)

    assert response.status_code == 200
    assert response.json() == {"task_id": "task-123", "cached": False, "message": "Task submitted successfully", "result": None}
    sent = queue.sent[0]
    assert base64.b64decode(sent["image_bytes"]) == IMAGE
    assert (sent["filename"], sent["content_type"], sent["image_hash"]) == ("fish.jpg", "image/jpeg", IMAGE_HASH)
    assert sent["image_path"] == "fish.jpg"  # the client's file name, not a path on the API host
    assert set(glob.glob("/tmp/fish_*")) == temp_files_before  # the upload is not written to disk
    metadata = json.loads(fake_redis.get("task_metadata:task-123"))
    assert metadata["cache_key"] == f"fish:{IMAGE_HASH}"
    assert 0 < fake_redis.ttl("task_metadata:task-123") <= 3600


def test_analyze_records_one_user_per_ip(client, queue, db):
    client.post("/analyze", files=UPLOAD)
    client.post("/analyze", files=UPLOAD)
    assert db.query(User).count() == 1

    client.post("/analyze", files=UPLOAD, headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    assert sorted(u.ip_address for u in db.query(User)) == ["203.0.113.7", "testclient"]


def test_analyze_cache_hit_returns_the_result_without_a_task(client, queue, fake_redis, db):
    fake_redis.set(f"fish:{IMAGE_HASH}", json.dumps({**RESULT, "cached_at": "2026-10-04T00:00:00"}))

    response = client.post("/analyze", files=UPLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["diagnosis"] == "fin_rot"
    assert body["detections"] == RESULT["detections"]
    assert "task_id" not in body
    assert queue.sent == []
    analysis = db.query(FishAnalysis).one()  # every upload is logged, also a cached one
    assert (analysis.total_objects, analysis.image_path) == (0, "fish.jpg")
    assert str(analysis.id) == body["id"]


def test_analyze_cache_hit_takes_the_photo_from_its_own_key(client, queue, fake_redis):
    cached = {k: v for k, v in RESULT.items() if k != "original_image"}
    fake_redis.set(f"fish:{IMAGE_HASH}", json.dumps({**cached, "image_hash": IMAGE_HASH}))
    fake_redis.set(f"photo:{IMAGE_HASH}", "c2VwYXJhdGU=")

    body = client.post("/analyze", files=UPLOAD).json()

    assert body["original_image"] == "c2VwYXJhdGU=" and "task_id" not in body
    assert queue.sent == []


def test_analyze_cached_result_that_lost_its_photo_is_analysed_again(client, queue, fake_redis):
    cached = {k: v for k, v in RESULT.items() if k != "original_image"}
    fake_redis.set(f"fish:{IMAGE_HASH}", json.dumps({**cached, "image_hash": IMAGE_HASH}))   # no photo key

    body = client.post("/analyze", files=UPLOAD).json()

    assert body["task_id"] == "task-123" and body["cached"] is False
    assert len(queue.sent) == 1


def test_analyze_rejects_a_photo_over_the_limit(client, queue, db):
    too_big = {"image": ("big.jpg", b"x" * (1024 * 1024 + 1), "image/jpeg")}   # MAX_UPLOAD_MB=1 in conftest

    response = client.post("/analyze", files=too_big)

    assert response.status_code == 413
    assert response.json() == {"detail": "Image is larger than 1 MB"}
    assert queue.sent == []
    assert db.query(User).count() == 0 and db.query(FishAnalysis).count() == 0


def test_analyze_accepts_a_photo_exactly_at_the_limit(client, queue):
    at_limit = {"image": ("big.jpg", b"x" * (1024 * 1024), "image/jpeg")}

    assert client.post("/analyze", files=at_limit).status_code == 200
    assert len(queue.sent) == 1


def test_analyze_queue_failure_hides_the_cause(client, queue):
    queue.fail = True

    response = client.post("/analyze", files=UPLOAD)

    assert response.status_code == 500
    assert response.json() == {"detail": "Task queue error"}
    assert "secret-broker-host" not in response.text


def test_analyze_without_a_task_id(client, queue):
    queue.task_id = None

    response = client.post("/analyze", files=UPLOAD)

    assert response.status_code == 500
    assert response.json() == {"detail": "couldnt get task_id"}


def test_result_of_an_unknown_task_is_processing(client):
    response = client.get("/analyze-result/nobody")

    assert response.status_code == 200
    assert response.json() == {
        "status": "processing",
        "message": "Image analysis is still in progress",
        "task_id": "nobody",
    }


def test_result_when_ready(client, fake_redis):
    fake_redis.set("result:abc", json.dumps(RESULT))   # the older format: the photo is inside the result

    body = client.get("/analyze-result/abc").json()

    assert body["id"] == "7" and body["diagnosis"] == "fin_rot"
    assert body["detections"] == RESULT["detections"]
    assert body["original_image"] == "ZmFrZQ=="
    assert (body["image_width"], body["image_height"]) == (640, 480)


def test_result_gets_its_photo_from_the_photo_key(client, fake_redis):
    result = {k: v for k, v in RESULT.items() if k != "original_image"}
    fake_redis.set("result:abc", json.dumps({**result, "image_hash": IMAGE_HASH}))
    fake_redis.set(f"photo:{IMAGE_HASH}", "c2VwYXJhdGU=")

    assert client.get("/analyze-result/abc").json()["original_image"] == "c2VwYXJhdGU="


def test_result_without_its_photo_is_still_delivered(client, fake_redis):
    result = {k: v for k, v in RESULT.items() if k != "original_image"}
    fake_redis.set("result:abc", json.dumps({**result, "image_hash": IMAGE_HASH}))   # photo evicted or expired

    body = client.get("/analyze-result/abc").json()

    assert body["diagnosis"] == "fin_rot" and body["original_image"] is None


def test_result_of_a_failed_task(client, fake_redis):
    fake_redis.set("error:abc", "ML service unavailable")

    assert client.get("/analyze-result/abc").json() == {
        "status": "failed",
        "message": "ML service unavailable",
        "task_id": "abc",
    }


def test_cancel_wins_over_a_finished_result(client, fake_redis):
    fake_redis.set("result:abc", json.dumps(RESULT))

    assert client.post("/cancel/abc").json() == {"status": "canceled"}

    assert 0 < fake_redis.ttl("cancel:abc") <= 3600
    body = client.get("/analyze-result/abc").json()
    assert body["status"] == "canceled" and "diagnosis" not in body


def test_cancel_does_not_check_that_the_task_exists(client):
    assert client.post("/cancel/never-submitted").json() == {"status": "canceled"}


def test_result_produced_by_the_worker_is_served_by_the_api(client, monkeypatch, tmp_path):
    monkeypatch.setattr(worker.requests, "post", lambda *a, **k: _Ok({
        "detections": [{"bbox": [1, 2, 3, 4], "det_confidence": 0.9, "class": "healthy", "class_confidence": 0.8,
                        "uncertain": False, "top3": []}],
        "image": "ZmFrZQ==", "image_width": 10, "image_height": 20,
    }))
    task = RabbitMQTask(task_id="task-9", result_queue="", image_path=str(tmp_path / "x.jpg"), filename="x.jpg",
                        image_bytes=base64.b64encode(IMAGE).decode(), image_hash=IMAGE_HASH, user_id=1)

    assert worker.Worker().process_task(task).success

    body = client.get("/analyze-result/task-9").json()
    assert body["diagnosis"] == "healthy"
    assert body["detections"][0]["detection_confidence"] == 0.9
    assert body["original_image"] == "ZmFrZQ=="   # the photo comes back although the result holds none
    # and the same photo is now answered from the cache, with its photo
    again = client.post("/analyze", files=UPLOAD).json()
    assert again["diagnosis"] == "healthy" and again["original_image"] == "ZmFrZQ=="


class _Ok:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_handshake_passes_the_health_answer_through(client, monkeypatch):
    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return type("R", (), {"text": '{"status":"healthy"}'})()

    monkeypatch.setattr(main.requests, "get", fake_get)

    assert client.get("/handshake").json() == {"response": '{"status":"healthy"}'}
    assert seen["url"] == "http://ml.test:8000/health"


def test_handshake_failure_hides_the_address(client, monkeypatch):
    def fake_get(url, timeout):
        raise ConnectionError("HTTPConnectionPool(host='secret-ml-host')")

    monkeypatch.setattr(main.requests, "get", fake_get)

    body = client.get("/handshake").json()
    assert body == {"error": "ML service unreachable"}


def test_cache_stats_and_clear_cache_touch_only_image_entries(client, fake_redis, monkeypatch):
    fake_redis.set("fish:aaa", json.dumps({"diagnosis": "healthy", "cached_at": "t1"}))
    fake_redis.set("fish:bbb", json.dumps({"diagnosis": "fin_rot", "cached_at": "t2"}))
    fake_redis.set("result:abc", json.dumps(RESULT))
    # fakeredis does not implement INFO
    monkeypatch.setattr(main.cache.client, "info", lambda section=None: {"used_memory_human": "1.2M"})

    stats = client.get("/cache-stats").json()
    assert stats["status"] == "connected" and stats["total_cached_images"] == 2
    assert stats["memory_used"] == "1.2M"
    assert {item["key"] for item in stats["example_cached_items"]} == {"fish:aaa", "fish:bbb"}

    assert client.get("/clear-cache").json() == {"status": "success", "cleared": 2}
    assert fake_redis.keys("*") == ["result:abc"]
