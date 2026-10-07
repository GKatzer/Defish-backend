"""Unit-level comparison of the worker before and after the fixes described in docs/design-decisions.md.

Runs four scenarios against the source tree in the current directory, with a fake Redis (fakeredis), a throw-away SQLite
database and a fake inference service; no Docker or broker is involved. Needs: pip install -r requirements.txt fakeredis

    git worktree add /tmp/before 2516efb        # the code before the fixes
    (cd /tmp/before && python /path/to/docs/examples/before_after_harness.py)
    python docs/examples/before_after_harness.py   # this version
"""
import base64, hashlib, os, sys, tempfile, json
os.environ["ML_SERVER_URL"] = "http://ml.test:8000"
os.environ["DATABASE_URL_INT"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.getcwd())
import logging, fakeredis, redis, requests, pika
logging.disable(logging.CRITICAL)   # keep the output to the four result lines
SERVER = fakeredis.FakeServer()
redis.Redis = lambda *a, **kw: fakeredis.FakeRedis(server=SERVER, **{k: v for k, v in kw.items() if k in ("decode_responses",)})
import worker
from database import Base, engine, SessionLocal
import models.models as M
Base.metadata.create_all(engine)

PHOTO = os.urandom(300_000)                       # a 300 KB "photo"
B64 = base64.b64encode(PHOTO).decode()
ML = {"detections": [
        {"bbox": [1, 2, 3, 4], "det_confidence": 0.91, "class": "fin_rot", "class_confidence": 0.88, "uncertain": False, "top3": []},
        {"bbox": [5, 6, 7, 8], "det_confidence": 0.55, "class": "healthy", "class_confidence": 0.6, "uncertain": True, "top3": []}],
      "image": B64, "image_width": 640, "image_height": 480}

class Resp:
    status_code = 200
    def json(self): return ML
requests.post = lambda *a, **k: Resp()

def task(task_id, image_path):
    from rabbitmq.schemas import RabbitMQTask
    return RabbitMQTask(task_id=task_id, result_queue="r", image_path=image_path, filename="f.jpg",
                        image_bytes=B64, image_hash=hashlib.md5(PHOTO).hexdigest(), user_id=1)

def db_counts():
    s = SessionLocal(); a = s.query(M.FishAnalysis).count(); d = s.query(M.Detection).count(); s.close(); return a, d

print("== 1. what is written to fish_analyses.processed_image_path")
bystander = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg"); bystander.write(b"x"); bystander.close()
worker.Worker().process_task(task("t1", bystander.name))
s = SessionLocal(); row = s.query(M.FishAnalysis).order_by(M.FishAnalysis.id.desc()).first()
v = row.processed_image_path
print("   processed_image_path:", "NULL" if v is None else f"{len(v):,} characters (the whole photo as base64)"); s.close()

print("== 2. file at task.image_path after the task")
print("   file still exists:", os.path.exists(bystander.name))

print("== 3. cancel arrives while the rows are being written")
a0, d0 = db_counts()
real = M.Detection
def detection_then_cancel(**kw):
    fakeredis.FakeRedis(server=SERVER).set("cancel:t3", "1"); return real(**kw)
M.Detection = detection_then_cancel
worker.Worker().process_task(task("t3", "f.jpg"))
M.Detection = real
a1, d1 = db_counts()
print(f"   rows left behind by the canceled task: fish_analyses +{a1 - a0}, detections +{d1 - d0}")

print("== 4. broker drops the connection while consuming")
class Ch:
    def queue_declare(self, **k): pass
    def basic_qos(self, **k): pass
    def basic_consume(self, **k): pass
    def start_consuming(self): raise pika.exceptions.ConnectionClosedByBroker(320, "CONNECTION_FORCED")
    def stop_consuming(self): pass
class Conn:
    is_closed = False; is_open = True
    def channel(self): return Ch()
    def close(self): self.is_closed = True
    def add_callback_threadsafe(self, cb): cb()
connects = []
w = worker.Worker()
def _connect():
    if len(connects) >= 3:
        w.should_stop = True      # end the experiment after three connection attempts
        return None
    connects.append(1)
    return Conn()
w._connect = _connect
worker.time.sleep = lambda s: None
worker.signal.signal = lambda *a: None
import threading
t = threading.Thread(target=w.start, daemon=True); t.start(); t.join(timeout=3)
print("   connections opened after the first drop:", len(connects), "(1 = the worker gave up and left its loop)")
w.should_stop = True
