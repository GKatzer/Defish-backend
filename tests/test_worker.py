import base64
import hashlib
import json

import pytest
import requests

import worker
from models.models import Detection, FishAnalysis
from rabbitmq.schemas import RabbitMQTask
from utils.recommendations import NO_FISH_RECOMMENDATION, RECOMMENDATIONS

IMAGE = b"fake-image-bytes"
IMAGE_HASH = hashlib.md5(IMAGE).hexdigest()

# Detections in the shape the inference service (Defish-inference, POST /analyze) returns them.
FIN_ROT = {
    "bbox": [10.0, 20.0, 110.0, 80.0],
    "det_confidence": 0.91,
    "class": "fin_rot",
    "class_confidence": 0.88,
    "uncertain": False,
    "top3": [
        {"label": "fin_rot", "confidence": 0.88},
        {"label": "healthy", "confidence": 0.07},
        {"label": "oodiniosis", "confidence": 0.03},
    ],
}
HEALTHY_UNSURE = {
    "bbox": [200.0, 40.0, 300.0, 120.0],
    "det_confidence": 0.55,
    "class": "healthy",
    "class_confidence": 0.6,
    "uncertain": True,
    "top3": [{"label": "healthy", "confidence": 0.6}],
}


def ml_payload(detections):
    return {"detections": detections, "image": "ZmFrZQ==", "image_width": 640, "image_height": 480}


def make_task(tmp_path, task_id="task-1"):
    return RabbitMQTask(
        task_id=task_id,
        result_queue="",
        image_path=str(tmp_path / "upload.jpg"),
        filename="upload.jpg",
        content_type="image/jpeg",
        image_bytes=base64.b64encode(IMAGE).decode(),
        image_hash=IMAGE_HASH,
        user_id=1,
    )


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def ml_returns(monkeypatch, payload, status=200, on_call=None):
    """Replace requests.post (the call to the inference service); returns the list of recorded calls."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        if on_call:
            on_call()
        return FakeResponse(payload, status)

    monkeypatch.setattr(worker.requests, "post", fake_post)
    return calls


def ml_raises(monkeypatch, exc):
    def fake_post(url, json=None, timeout=None):
        raise exc

    monkeypatch.setattr(worker.requests, "post", fake_post)


def test_success_stores_result_cache_and_rows(monkeypatch, tmp_path, fake_redis, db):
    calls = ml_returns(monkeypatch, ml_payload([FIN_ROT, HEALTHY_UNSURE]))
    task = make_task(tmp_path)

    result = worker.Worker().process_task(task)

    assert result.success
    assert calls[0]["url"] == "http://ml.test:8000/analyze"
    assert calls[0]["timeout"] == 300  # the documented limit of the call to the inference service
    assert calls[0]["json"]["image_bytes"] == task.image_bytes

    payload = json.loads(fake_redis.get("result:task-1"))
    assert payload["diagnosis"] == "fin_rot, healthy"
    assert payload["confidence"] == pytest.approx((0.88 + 0.6) / 2)
    assert payload["recommendations"] == RECOMMENDATIONS["fin_rot"]
    assert (payload["image_width"], payload["image_height"]) == (640, 480)

    first, second = payload["detections"]
    assert (first["x_min"], first["y_min"], first["x_max"], first["y_max"]) == (10.0, 20.0, 110.0, 80.0)
    assert first["classification_class"] == "fin_rot"
    assert first["classification_confidence"] == 0.88
    assert first["recommendations"] == RECOMMENDATIONS["fin_rot"]
    # the service sends det_confidence; it used to be read under another name and always came out as 0.0
    assert first["detection_confidence"] == 0.91
    assert first["uncertain"] is False
    assert [item["label"] for item in first["top3"]] == ["fin_rot", "healthy", "oodiniosis"]
    assert second["uncertain"] is True

    analysis = db.query(FishAnalysis).one()
    assert analysis.total_objects == 2
    assert str(analysis.id) == payload["id"]
    assert analysis.processed_image_path is None  # the photo itself is not stored in the table
    assert analysis.image_path == str(tmp_path / "upload.jpg")
    rows = db.query(Detection).order_by(Detection.id).all()
    assert [r.classification_class for r in rows] == ["fin_rot", "healthy"]
    assert rows[0].detection_confidence == 0.91

    cached = json.loads(fake_redis.get(f"fish:{IMAGE_HASH}"))
    assert cached["id"] == payload["id"]
    assert cached["detections"] == payload["detections"]
    # the photo is stored once, under its own key, not inside the result and the cache entry
    assert "original_image" not in payload and "original_image" not in cached
    assert payload["image_hash"] == cached["image_hash"] == IMAGE_HASH
    assert fake_redis.get(f"photo:{IMAGE_HASH}") == "ZmFrZQ=="
    assert 0 < fake_redis.ttl(f"photo:{IMAGE_HASH}") <= 120
    assert 0 < fake_redis.ttl(f"fish:{IMAGE_HASH}") <= 120  # CACHE_SAVING_TIME set in conftest
    assert 0 < fake_redis.ttl("result:task-1") <= 3600
    assert fake_redis.get("error:task-1") is None


def test_no_fish_is_not_reported_as_healthy(monkeypatch, tmp_path, fake_redis, db):
    ml_returns(monkeypatch, ml_payload([]))

    result = worker.Worker().process_task(make_task(tmp_path))

    assert result.success
    payload = json.loads(fake_redis.get("result:task-1"))
    assert payload["diagnosis"] == "No objects detected"
    assert payload["confidence"] == 0.0
    assert payload["detections"] == []
    assert payload["recommendations"] == NO_FISH_RECOMMENDATION
    assert db.query(FishAnalysis).one().total_objects == 0
    assert db.query(Detection).count() == 0


def test_legacy_detection_field_names_are_still_understood():
    legacy = {
        "x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4,
        "detection_confidence": 0.7, "classification_class": "oodiniosis", "confidence": 0.5,
    }
    parsed = worker.parse_detection(legacy)
    assert (parsed["x_min"], parsed["y_max"]) == (1, 4)
    assert parsed["detection_confidence"] == 0.7
    assert parsed["classification_class"] == "oodiniosis"
    assert parsed["classification_confidence"] == 0.5
    assert parsed["uncertain"] is False and parsed["top3"] == []


def test_ml_http_error_is_reported_without_details(monkeypatch, tmp_path, fake_redis, db):
    ml_returns(monkeypatch, {}, status=500)

    result = worker.Worker().process_task(make_task(tmp_path))

    assert not result.success
    assert fake_redis.get("error:task-1") == "ML service returned HTTP 500"
    assert fake_redis.get("result:task-1") is None
    assert db.query(FishAnalysis).count() == 0


def test_ml_unreachable_hides_the_address_from_the_client(monkeypatch, tmp_path, fake_redis):
    ml_raises(monkeypatch, requests.ConnectionError("HTTPConnectionPool(host='secret-ml-host', port=8000): refused"))

    result = worker.Worker().process_task(make_task(tmp_path))

    assert fake_redis.get("error:task-1") == "ML service unavailable"
    assert "secret-ml-host" in result.error  # the detail stays in the worker's own result and logs


def test_ml_timeout(monkeypatch, tmp_path, fake_redis):
    ml_raises(monkeypatch, requests.Timeout("read timed out"))

    worker.Worker().process_task(make_task(tmp_path))

    assert fake_redis.get("error:task-1") == "ML service timed out"


def test_unexpected_error_is_reported_as_internal(monkeypatch, tmp_path, fake_redis):
    ml_raises(monkeypatch, KeyError("secret"))

    worker.Worker().process_task(make_task(tmp_path))

    assert fake_redis.get("error:task-1") == "Internal error"


def test_cancel_before_start_skips_the_ml_call(monkeypatch, tmp_path, fake_redis, db):
    calls = ml_returns(monkeypatch, ml_payload([FIN_ROT]))
    fake_redis.set("cancel:task-1", "1")

    result = worker.Worker().process_task(make_task(tmp_path))

    assert (result.success, result.error) == (False, "canceled")
    assert calls == []
    assert fake_redis.get("result:task-1") is None
    assert fake_redis.get(f"photo:{IMAGE_HASH}") is None
    assert fake_redis.get("error:task-1") is None
    assert db.query(FishAnalysis).count() == 0


def test_cancel_during_the_ml_call_skips_saving(monkeypatch, tmp_path, fake_redis, db):
    ml_returns(monkeypatch, ml_payload([FIN_ROT]), on_call=lambda: fake_redis.set("cancel:task-1", "1"))

    result = worker.Worker().process_task(make_task(tmp_path))

    assert (result.success, result.error) == (False, "canceled")
    assert fake_redis.get("result:task-1") is None
    assert fake_redis.get(f"fish:{IMAGE_HASH}") is None
    assert fake_redis.get(f"photo:{IMAGE_HASH}") is None
    assert db.query(FishAnalysis).count() == 0
    assert db.query(Detection).count() == 0


def test_cancel_after_the_rows_were_written_leaves_nothing_behind(monkeypatch, tmp_path, fake_redis, db):
    import models.models

    ml_returns(monkeypatch, ml_payload([FIN_ROT, HEALTHY_UNSURE]))
    real_detection = models.models.Detection

    def detection_then_cancel(**fields):
        # the user presses "cancel" while the worker is writing the rows
        fake_redis.set("cancel:task-1", "1")
        return real_detection(**fields)

    monkeypatch.setattr(models.models, "Detection", detection_then_cancel)

    result = worker.Worker().process_task(make_task(tmp_path))

    assert (result.success, result.error) == (False, "canceled")
    assert fake_redis.get("result:task-1") is None
    assert fake_redis.get(f"fish:{IMAGE_HASH}") is None
    assert fake_redis.get(f"photo:{IMAGE_HASH}") is None
    assert db.query(FishAnalysis).count() == 0
    assert db.query(real_detection).count() == 0


def test_the_worker_does_not_delete_files_named_like_the_upload(monkeypatch, tmp_path, fake_redis):
    ml_returns(monkeypatch, ml_payload([FIN_ROT]))
    bystander = tmp_path / "upload.jpg"  # the task's image_path is the client's file name
    bystander.write_bytes(b"not yours to delete")

    assert worker.Worker().process_task(make_task(tmp_path)).success

    assert bystander.read_bytes() == b"not yours to delete"


def test_without_an_image_in_the_ml_answer_the_uploaded_photo_is_returned(monkeypatch, tmp_path, fake_redis):
    payload = ml_payload([FIN_ROT])
    del payload["image"]
    ml_returns(monkeypatch, payload)
    task = make_task(tmp_path)

    worker.Worker().process_task(task)

    assert fake_redis.get(f"photo:{IMAGE_HASH}") == task.image_bytes
