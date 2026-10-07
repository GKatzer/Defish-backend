# rabbitmq/schemas.py
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime
import uuid

# A single timeout (seconds) for the whole analysis cycle: RabbitMQ send/wait + the HTTP request to the ml server
ML_ANALYZE_TIMEOUT = 300

class RabbitMQTask(BaseModel):
    """Full task for RabbitMQ"""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Queue for the worker's reply. Empty for now: the result goes through Redis. The field is kept
    # so that the worker understands messages queued by an older version of the API.
    result_queue: str = ""
    image_path: str  # name of the uploaded file (this used to be the path of a temporary file of the API)
    filename: str
    content_type: str = "image/jpeg"
    image_bytes: str
    image_hash: str
    user_id: int
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    timeout: int = Field(default=ML_ANALYZE_TIMEOUT, ge=10, le=300)  # seconds

class TaskResult(BaseModel):
    """Result of a task, from the Worker"""
    task_id: str
    success: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    processing_time: Optional[float] = None
    processed_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    

# Export all schemas
__all__ = ["RabbitMQTask", "TaskResult", "ML_ANALYZE_TIMEOUT"]