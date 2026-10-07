# worker.py (in the project root)
"""
RabbitMQ worker that processes fish analysis tasks.
Uses the schemas from the rabbitmq module.
"""
import pika
import json
import requests
import os
import logging
import time
import signal
from datetime import datetime
from typing import Optional
from utils.cache import RedisCache, make_redis
from utils.recommendations import get_recommendations, summarize_recommendations
from config import ML_SERVER_URL, CACHE_SAVING_TIME, TASK_KEY_TTL

from rabbitmq.schemas import RabbitMQTask, TaskResult, ML_ANALYZE_TIMEOUT

# Logger setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Detection fields that are written to the detections table.
# uncertain and top3 go only to the API answer (there are no columns for them).
DB_DETECTION_FIELDS = (
    "x_min", "y_min", "x_max", "y_max",
    "detection_class", "detection_confidence",
    "classification_class", "classification_confidence",
    "recommendations",
)


class MLServiceError(Exception):
    """The ML service answered with something other than 200."""

    def __init__(self, status_code: int):
        super().__init__(f"ML error {status_code}")
        self.status_code = status_code


def parse_detection(det: dict) -> dict:
    """One detection from the ML service's answer -> a record for the API answer and the detections table.

    The ML service returns bbox, det_confidence, class, class_confidence, uncertain, top3.
    The old field names (x_min..y_max, detection_confidence, classification_class, confidence)
    are understood too.
    """
    bbox = det.get("bbox") or []

    def coord(name: str, index: int):
        return det.get(name, bbox[index] if len(bbox) > index else 0.0)

    class_name = str(det.get("class", det.get("classification_class", "unknown")))
    return {
        "x_min": coord("x_min", 0),
        "y_min": coord("y_min", 1),
        "x_max": coord("x_max", 2),
        "y_max": coord("y_max", 3),
        "detection_class": str(det.get("detection_class", "0")),
        "detection_confidence": float(det.get("det_confidence", det.get("detection_confidence", 0.0))),
        "classification_class": class_name,
        "classification_confidence": float(det.get("class_confidence", det.get("confidence", 0.0))),
        "recommendations": get_recommendations(class_name),
        "uncertain": bool(det.get("uncertain", False)),
        "top3": det.get("top3", []),
    }


def public_error(exc: Exception) -> str:
    """Short error text for the client.

    Without addresses or details of internal services: those stay in the worker's log.
    """
    if isinstance(exc, requests.Timeout):
        return "ML service timed out"
    if isinstance(exc, requests.ConnectionError):
        return "ML service unavailable"
    if isinstance(exc, MLServiceError):
        return f"ML service returned HTTP {exc.status_code}"
    return "Internal error"


class Worker:
    """Worker that processes tasks from RabbitMQ"""

    def __init__(self):
        self.host = os.getenv('RABBITMQ_HOST', 'localhost')
        self.port = int(os.getenv('RABBITMQ_PORT', 5672))
        self.username = os.getenv('RABBITMQ_USERNAME', 'guest')
        self.password = os.getenv('RABBITMQ_PASSWORD', 'guest')
        self.ml_server = ML_SERVER_URL + "/analyze"
        self.queue_name = 'fish_analysis_queue'

        # Flag for graceful shutdown
        self.should_stop = False

        # Current RabbitMQ session (needed by the signal handler)
        self._connection = None
        self._channel = None

        # Statistics
        self.stats = {
            "processed": 0,
            "failed": 0,
            "started_at": datetime.now().isoformat()
        }

    def _connect(self) -> Optional[pika.BlockingConnection]:
        """Connecting to RabbitMQ"""
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=self.host,
                    port=self.port,
                    credentials=pika.PlainCredentials(self.username, self.password),
                    heartbeat=600,
                    blocked_connection_timeout=300
                )
            )
            return connection
        except Exception as e:
            logger.error(f"❌ RabbitMQ connection error: {e}")
            return None

    def process_task(self, task: RabbitMQTask) -> TaskResult:
        task_id = task.task_id
        redis_client = make_redis(decode_responses=True)
        image_bytes = task.image_bytes
        start_time = time.time()
        cancel_key = f"cancel:{task_id}"
        db = None

        try:
            logger.info(f"Started processing task {task_id}")

            try:
                if redis_client.get(cancel_key):
                    logger.info(f"Task {task_id} was canceled before processing began, skipping")
                    return TaskResult(
                        task_id=task_id,
                        success=False,
                        error="canceled"
                    )
            except Exception as e:
                logger.warning(f"Could not check whether task {task_id} was canceled before the start, continuing: {e}")

            # ===== 1. Call the ML service =====
            response = requests.post(
                self.ml_server,
                json={
                    "image_bytes": image_bytes,
                    "filename": task.filename,
                    "content_type": task.content_type,
                    "user_id": task.user_id
                },
                timeout=ML_ANALYZE_TIMEOUT
            )

            if response.status_code != 200:
                raise MLServiceError(response.status_code)

            logger.info(f"image: {image_bytes[:8]}")

            ml_result = response.json()

            try:
                if redis_client.get(cancel_key):
                    logger.info(f"Task {task_id} was canceled during the ML call, skipping the save")
                    return TaskResult(
                        task_id=task_id,
                        success=False,
                        error="canceled"
                    )
            except Exception as e:
                logger.warning(f"Could not check whether task {task_id} was canceled after the ML call, continuing: {e}")

            # ===== 2. Process the detections =====
            detections = [parse_detection(det) for det in ml_result.get("detections", [])]
            class_names = [det["classification_class"] for det in detections]
            confidence_list = [det["classification_confidence"] for det in detections]

            avg_confidence = (
                sum(confidence_list) / len(confidence_list)
                if confidence_list else 0.0
            )

            diagnosis = ", ".join(class_names) if class_names else "No objects detected"

            # ===== 3. Save to the database =====
            from database import SessionLocal
            from models.models import FishAnalysis, Detection

            db = SessionLocal()

            analysis = FishAnalysis(
                user_id=task.user_id,
                processed_image_path=None,  # there is no processed image; the photo itself is not stored in the DB
                total_objects=len(detections),
                image_path=task.image_path,
            )

            # logger.info(f"Adding to db {analysis}")

            db.add(analysis)
            db.flush()  # get analysis.id without committing: on cancellation nothing is left in the DB

            for det in detections:
                detection = Detection(
                    analysis_id=analysis.id,
                    **{field: det[field] for field in DB_DETECTION_FIELDS}
                )
                db.add(detection)

            try:
                if redis_client.get(cancel_key):
                    logger.info(f"Task {task_id} canceled, skipping result save")
                    db.rollback()
                    db.close()
                    return TaskResult(
                        task_id=task_id,
                        success=False,
                        error="canceled"
                        )
            except Exception as e:
                logger.warning(f"Could not check whether task {task_id} was canceled, continuing: {e}")

            # ===== 4. Save the final result in Redis =====
            # The photo is in Redis once (photo:<md5>), not inside every result: otherwise the picture
            # takes memory twice (cache and result), and Redis evicts results.
            image_hash = task.image_hash
            photo = ml_result.get("image", task.image_bytes)
            result_payload = {
                "id": str(analysis.id),
                "diagnosis": diagnosis,
                "confidence": avg_confidence,
                "recommendations": summarize_recommendations(class_names),
                "image_hash": image_hash,
                "image_format": "jpeg",
                "detections": detections,
                "image_width": ml_result.get("image_width", 0),
                "image_height": ml_result.get("image_height", 0),
            }

            db.commit()
            db.close()

            cache = RedisCache()

            cache_data = {
                **result_payload,
                "cached_at": datetime.now().isoformat(),
            }

            # photo first, then the result: whoever sees the result finds the photo too
            cache.setex(f"photo:{image_hash}", CACHE_SAVING_TIME, photo)
            cache.setex(f"fish:{image_hash}", CACHE_SAVING_TIME, json.dumps(cache_data))
            logger.info(f"💾 Saved to the cache for {CACHE_SAVING_TIME} s")

            redis_client.setex(
                f"result:{task_id}",
                TASK_KEY_TTL,
                json.dumps(result_payload)
            )

            logger.info(f"Task {task_id} fully completed")

            return TaskResult(
                task_id=task_id,
                success=True,
                result=result_payload,
                processing_time=time.time() - start_time,
                processed_at=datetime.now().isoformat()
            )

        except Exception as e:
            logger.exception("Processing error")
            try:
                redis_client.setex(f"error:{task_id}", TASK_KEY_TTL, public_error(e))
            except Exception:
                logger.error(f"Could not save the error status for {task_id} in Redis")
            return TaskResult(
                task_id=task_id,
                success=False,
                error=str(e),
                processed_at=datetime.now().isoformat()
            )
        finally:
            if db is not None:
                db.close()

    def _on_message(self, ch, method, properties, body):
        """Callback for processing messages"""
        task = None

        try:
            # Parse the task
            task_data = json.loads(body)
            task = RabbitMQTask(**task_data)

            # Process the task
            result = self.process_task(task)

            # Send the result back if a queue is given
            if task.result_queue:
                ch.basic_publish(
                    exchange='',
                    routing_key=task.result_queue,
                    body=json.dumps(result.model_dump()),
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        content_type='application/json',
                        correlation_id=task.task_id
                    )
                )

            # Acknowledge the processing
            ch.basic_ack(delivery_tag=method.delivery_tag)

            # Update the statistics
            if result.success:
                self.stats["processed"] += 1
            else:
                self.stats["failed"] += 1

            logger.info(f"✅ Task {task.task_id} completed. Statistics: {self.stats}")

        except json.JSONDecodeError as e:
            logger.error(f"❌ Error parsing the task JSON: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

        except Exception as e:
            logger.error(f"❌ Critical error in the callback: {e}", exc_info=True)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def _consume(self, connection):
        """One consuming session: blocks until the connection drops or the worker is stopped."""
        channel = connection.channel()

        # Declare the queue (the same as in the client)
        channel.queue_declare(
            queue=self.queue_name,
            durable=True
        )

        # Quality of service setup (one task at a time)
        channel.basic_qos(prefetch_count=1)

        # Start listening to the queue
        channel.basic_consume(
            queue=self.queue_name,
            on_message_callback=self._on_message,
            auto_ack=False
        )

        self._connection, self._channel = connection, channel
        if self.should_stop:  # the signal arrived while connecting
            return

        logger.info("👷 Worker started and is waiting for tasks...")
        logger.info("ℹ️  Press Ctrl+C to stop")
        # Start consuming (blocking call)
        channel.start_consuming()

    def _stop_consuming(self):
        """Asks the current consuming session to finish: after the current task is processed."""
        connection, channel = self._connection, self._channel
        if connection is not None and channel is not None and connection.is_open:
            connection.add_callback_threadsafe(channel.stop_consuming)

    def _close(self, connection):
        self._connection = self._channel = None
        if connection and not connection.is_closed:
            try:
                connection.close()
                logger.info("🔌 Connection to RabbitMQ closed")
            except Exception:
                pass

    def start(self):
        """Start the worker"""
        logger.info("🚀 Starting RabbitMQ Worker...")
        logger.info(f"📍 RabbitMQ: {self.host}:{self.port}")
        logger.info(f"🤖 ML server: {self.ml_server}")
        logger.info(f"📂 Queue: {self.queue_name}")

        # Signal handling for graceful shutdown: the current task is finished, then exit
        def signal_handler(sig, frame):
            logger.info(f"📩 Received signal {sig}, graceful shutdown...")
            self.should_stop = True
            self._stop_consuming()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            while not self.should_stop:
                # Connect / reconnect
                connection = self._connect()
                if not connection:
                    logger.error("Could not connect to RabbitMQ, retrying in 10 s...")
                    time.sleep(10)
                    continue

                try:
                    self._consume(connection)

                except pika.exceptions.ConnectionClosedByBroker:
                    logger.info("🔌 Connection closed by the broker, reconnecting...")
                    time.sleep(5)

                except pika.exceptions.AMQPConnectionError:
                    logger.error("🔌 RabbitMQ connection error, reconnecting...")
                    time.sleep(10)

                finally:
                    self._close(connection)

        except KeyboardInterrupt:
            logger.info("\n🛑 Stopped at the user's request")

        except Exception as e:
            logger.error(f"🚨 Critical worker error: {e}", exc_info=True)

        finally:
            # Print the final statistics
            total_tasks = self.stats["processed"] + self.stats["failed"]
            logger.info("📊 Final statistics:")
            logger.info(f"   Total tasks: {total_tasks}")
            logger.info(f"   Succeeded: {self.stats['processed']}")
            logger.info(f"   Failed: {self.stats['failed']}")
            logger.info(f"   Uptime: {self.stats['started_at']} - {datetime.now().isoformat()}")
            logger.info("👋 Worker stopped")

def main():
    """Entry point"""
    worker = Worker()
    worker.start()

if __name__ == "__main__":
    main()
