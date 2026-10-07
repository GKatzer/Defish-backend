# main.py
from sqlalchemy.orm import Session
from database import get_db, engine
from models.models import Base, User, FishAnalysis, AnalysisResponse, TaskResponse,\
AnalyzeResultResponse, PostResponses
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
import requests
import hashlib
import json
import base64
from datetime import datetime
import logging
from rabbitmq import get_async_rabbitmq_client, ML_ANALYZE_TIMEOUT
from utils.cache import RedisCache
from config import ML_SERVER_URL, TASK_KEY_TTL, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB

# Logger setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Defish API")

# tables
Base.metadata.create_all(bind=engine)

# CORS frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# cache
cache = RedisCache()

def get_client_ip(request: Request) -> str:
    try:
        # print(request.headers)
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()

        x_real_ip = request.headers.get("x-real-ip")
        if x_real_ip:
            return x_real_ip

        return request.client.host

    except Exception as e:
        logger.error(f"Get clint ip error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Get client ip error"
        )

def load_photo(image_hash):
    """The photo of an analysis is kept in Redis apart from the result (photo:<md5>); None if it is already gone."""
    if not image_hash:
        return None
    photo = cache.get(f"photo:{image_hash}")
    return photo.decode() if isinstance(photo, bytes) else photo

@app.get("/handshake")
def call_ml_server():
    """Check the connection to the ML server"""
    try:
        response = requests.get(ML_SERVER_URL + "/health", timeout=5)
        return {"response": response.text}
    except Exception as e:
        logger.warning(f"Handshake with ML server failed: {e}")
        return {"error": "ML service unreachable"}

@app.post("/analyze", response_model=PostResponses)
async def analyze_fish(
    request: Request,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    logger.info("start analyze")
    try:

        # read at most the limit + 1 byte: the whole file sits in memory, and the container's memory limit is small
        image_bytes = await image.read(MAX_UPLOAD_BYTES + 1)
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"Image is larger than {MAX_UPLOAD_MB:g} MB")
        # Create hash
        image_hash = hashlib.md5(image_bytes).hexdigest()
        cache_key = f"fish:{image_hash}"
        # Get IP
        client_ip = get_client_ip(request)

        logger.info("db query")

        user = db.query(User).filter(User.ip_address == client_ip).first()
        if not user:
            user = User(ip_address=client_ip)
            db.add(user)
            db.commit()
            db.refresh(user)

        logger.info("cache")

        # Check Redis cache
        cached_result = cache.get(cache_key)
        result = json.loads(cached_result) if cached_result else None
        # cache entries of the old format keep the photo inside, new ones keep it under a separate key
        original_image = (result.get("original_image") or load_photo(image_hash)) if result else None
        if result and not original_image:
            logger.info(f"Cached result has lost its photo, analysing again (hash: {image_hash[:8]}...)")
        if result and original_image:
            logger.info(f"Get result from cache! (hash: {image_hash[:8]}...)")

            analysis = FishAnalysis(
                user_id=user.id,
                processed_image_path=None,
                total_objects=0,
                image_path=image.filename,
            )
            db.add(analysis)
            db.commit()
            db.refresh(analysis)
            return AnalysisResponse(
                id=str(analysis.id),
                diagnosis=result["diagnosis"],
                confidence=float(result["confidence"]),
                recommendations=result["recommendations"],
                original_image=original_image,
                image_format=result.get("image_format", "jpeg"),
                image_width=result.get("image_width", 0),
                image_height=result.get("image_height", 0),
                detections=result.get("detections", []),
            )
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        try:
            rabbitmq = await get_async_rabbitmq_client()
            try:
                task_id = await rabbitmq.send(
                    task_data={
                        "image_bytes": image_base64,
                        "image_path": image.filename,
                        "filename": image.filename,
                        "content_type": image.content_type or "image/jpeg",
                        "image_hash": image_hash,
                        "user_id": user.id,
                        "created_at": datetime.now().isoformat()
                    },
                    timeout=ML_ANALYZE_TIMEOUT
                )
            except Exception as e:
                logger.exception(f"RabbitMQ sending error: {e}")
                raise HTTPException(status_code=500, detail="Task queue error")
            if not task_id:
                raise HTTPException(500, "couldnt get task_id")

            metadata_key = f"task_metadata:{task_id}"
            metadata = {
                "user_id": user.id,
                "image_path": image.filename,
                "cache_key": cache_key,
                "image_hash": image_hash,
                "filename": image.filename,
                "content_type": image.content_type,
                "created_at": datetime.now().isoformat()
            }

            cache.setex(metadata_key, TASK_KEY_TTL, json.dumps(metadata))
            logger.info(f"Saved metadata for task {task_id} in Redis")

            return TaskResponse(
                task_id=task_id,
                cached=False,
                message="Task submitted successfully"
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"RabbitMQ client error: {e}")
            raise HTTPException(status_code=500, detail="Task queue error")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"CRASH HERE. {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )

@app.post("/cancel/{task_id}")
async def cancel_task(task_id: str):
    cancel_key = f"cancel:{task_id}"
    cache.setex(cancel_key, TASK_KEY_TTL, "1")

    return {"status": "canceled"}

@app.get("/analyze-result/{task_id}", response_model=AnalyzeResultResponse)
async def analyze_result(task_id: str):
    logger.info(f"Getting result for task: {task_id}")

    cancel_key = f"cancel:{task_id}"
    if cache.get(cancel_key):
        logger.info(f"Task: {task_id} is cancelled")
        return {
            "status": "canceled",
            "task_id": task_id,
            "message": "Analysis canceled by user"
        }

    error_key = f"error:{task_id}"
    error_message = cache.get(error_key)
    if error_message:
        if isinstance(error_message, bytes):
            error_message = error_message.decode()
        logger.info(f"Task: {task_id} failed: {error_message}")
        return {
            "status": "failed",
            "task_id": task_id,
            "message": error_message
        }

    result_key = f"result:{task_id}"
    result_data = cache.get(result_key)

    if not result_data:
        logger.info(f"Result not ready yet for task: {task_id}")
        return {
            "status": "processing",
            "message": "Image analysis is still in progress",
            "task_id": task_id,
        }

    result = json.loads(result_data)

    logger.info(f"Returning result for task: {task_id}")

    return AnalysisResponse(
        id=result["id"],
        diagnosis=result["diagnosis"],
        confidence=result["confidence"],
        recommendations=result["recommendations"],
        original_image=result.get("original_image") or load_photo(result.get("image_hash")),
        image_format=result.get("image_format", "jpeg"),
        image_width=result.get("image_width", 0),
        image_height=result.get("image_height", 0),
        detections=result.get("detections", []),
    )


@app.get("/cache-stats")
async def get_cache_stats():
    """
    Cache statistics
    """
    if not cache.client:
        return {"status": "Redis not connected"}

    try:
        # Count keys with "fish":
        keys = cache.client.keys("fish:*")

        # Get some examples
        example_data = []
        for key in keys[:3]:
            data = cache.client.get(key)
            if data:
                parsed = json.loads(data)
                example_data.append({
                    "key": key.decode('utf-8') if isinstance(key, bytes) else key,
                    "diagnosis": parsed.get("diagnosis"),
                    "cached_at": parsed.get("cached_at")
                })

        return {
            "status": "connected",
            "total_cached_images": len(keys),
            "memory_used": cache.client.info('memory').get('used_memory_human', 'N/A'),
            "example_cached_items": example_data
        }
    except Exception as e:
        logger.warning(f"cache-stats failed: {e}")
        return {"status": "error"}

@app.get("/clear-cache")
async def clear_cache():
    """
    Clear all cache
    """
    if not cache.client:
        return {"status": "Redis not connected"}

    try:
        keys = cache.client.keys("fish:*")
        if keys:
            cache.client.delete(*keys)
        return {"status": "success", "cleared": len(keys)}
    except Exception as e:
        logger.warning(f"clear-cache failed: {e}")
        return {"status": "error"}

@app.get("/")
async def root():
    return {"message": "Defish API is running"}
