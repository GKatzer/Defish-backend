"""
Asynchronous client for RabbitMQ.
"""

import aio_pika
import json
import uuid
import logging
import os
from typing import Optional, Dict, Any

from .schemas import RabbitMQTask, ML_ANALYZE_TIMEOUT

logger = logging.getLogger(__name__)


class AsyncRabbitMQClient:
    """Asynchronous client for RabbitMQ"""

    def __init__(self):
        self.connection = None
        self.channel = None
        self.connected = False
        self.host = os.getenv('RABBITMQ_HOST', 'localhost')
        self.port = int(os.getenv('RABBITMQ_PORT', 5672))
        self.username = os.getenv('RABBITMQ_USERNAME', 'guest')
        self.password = os.getenv('RABBITMQ_PASSWORD', 'guest')
        self.queue_name = 'fish_analysis_queue'


    async def connect(self):
        """Asynchronous connection to RabbitMQ"""
        if self.connected and self.connection and not self.connection.is_closed:
            return True

        try:
            self.connection = await aio_pika.connect_robust(
                host=self.host,
                port=self.port,
                login=self.username,
                password=self.password,
            )

            self.channel = await self.connection.channel()

            # Declare the main queue
            await self.channel.declare_queue(
                self.queue_name,
                durable=True
            )

            self.connected = True
            logger.info(f"✅ AsyncRabbitMQ connected to {self.host}:{self.port}")
            return True

        except Exception as e:
            logger.error(f"❌ RabbitMQ connection error: {e}")
            self.connected = False
            return False

    async def send(
        self,
        task_data: Dict[str, Any],
        timeout: int = ML_ANALYZE_TIMEOUT,
    ) -> Optional[str]:
        """Puts a task in the queue and returns the task_id (None if it could not be published).

        The result does not come back through RabbitMQ: the worker puts it in Redis (result:<task_id>),
        and the API client reads it from there.
        """
        if not await self.connect():
            raise ConnectionError("Could not connect to RabbitMQ")

        task_id = str(uuid.uuid4())

        try:
            full_task = RabbitMQTask(
                task_id=task_id,
                timeout=timeout,
                **task_data
            )

            logger.info(
                "Task publish:\n"
                f"routing_key={self.queue_name}\n"
                f"correlation_id={task_id}"
            )

            await self.channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(full_task.model_dump()).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type='application/json',
                    correlation_id=task_id,
                ),
                routing_key=self.queue_name
            )

            logger.info(f"📤 Task sent: {task_id}")
            return task_id

        except Exception as e:
            logger.error(f"❌ Error while sending: {e}")
            return None

    async def close(self):
        """Closing the connection"""
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            logger.info("🔌 Async connection to RabbitMQ closed")
        self.connected = False


# Singleton instance
_async_rabbitmq_client_instance = None


async def get_async_rabbitmq_client() -> AsyncRabbitMQClient:
    """Factory for getting the asynchronous instance"""
    global _async_rabbitmq_client_instance
    if _async_rabbitmq_client_instance is None:
        _async_rabbitmq_client_instance = AsyncRabbitMQClient()
    return _async_rabbitmq_client_instance
