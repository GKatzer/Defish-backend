from .rabbitmq_client import AsyncRabbitMQClient, get_async_rabbitmq_client
from .schemas import RabbitMQTask, TaskResult, ML_ANALYZE_TIMEOUT

__version__ = "1.1.0"  # Version bumped because the asynchronous functionality was added
__all__ = [
    "AsyncRabbitMQClient",
    "get_async_rabbitmq_client",
    "RabbitMQTask",
    "TaskResult",
    "ML_ANALYZE_TIMEOUT"
]