"""Stand-in for the inference service (Defish-inference), so the API can run without model weights.

It implements the two routes the API calls, GET /health and POST /analyze, with the request and response
shapes of the real service. The detections are canned: derived from the hash of the image, not from looking at it.
/health says "mock": true. Do not use this to judge model quality.

The API passes the uploaded file name through to /analyze, which allows a few switches:
    *slow*    answers after MOCK_DELAY_SECONDS (default 8): to try polling and cancelling
    *broken*  answers HTTP 500: to see a failed task
    *empty*   finds no fish

MOCK_USE_LAYOUT=1 makes the mock return, for exactly the sample picture next to this file (aquarium-synthetic.jpg), boxes on the four
fish that were drawn into it, with the classes listed in aquarium-synthetic.layout.json. The boxes are known from the drawing, not found
by looking at the picture, and the classes are arbitrary; it exists so that screenshots of a client show boxes on the fish.

    python docs/examples/mock_ml_server.py          # 127.0.0.1:8000; MOCK_HOST, MOCK_PORT change it
"""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

CLASSES = ["healthy", "fin_rot", "dermatomycosis", "hexamitosis", "mycobacteriosis", "oodiniosis", "plistophorosis"]
UNCERTAIN_BELOW = 0.83  # the real service's confidence gate (models/disease_classifier/meta.json)
DELAY_SECONDS = float(os.getenv("MOCK_DELAY_SECONDS", "8"))
USE_LAYOUT = os.getenv("MOCK_USE_LAYOUT") == "1"
HERE = Path(__file__).parent

app = FastAPI(title="Mock inference service")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "ml-inference", "version": "1.0.0",
            "detector": "loaded", "classifier": "loaded", "mock": True}


def image_size(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return 640, 480


def canned_detections(digest: bytes, width: int, height: int) -> list[dict]:
    fish = []
    for i in range(digest[0] % 3 + 1):  # one to three fish
        b = digest[1 + i * 6: 7 + i * 6]
        x1, y1 = int(b[0] / 255 * width * 0.5), int(b[1] / 255 * height * 0.5)
        x2, y2 = x1 + int(width * (0.15 + b[2] / 255 * 0.2)), y1 + int(height * (0.1 + b[3] / 255 * 0.15))
        top = sorted(range(len(CLASSES)), key=lambda c: digest[(7 + c + i) % 16], reverse=True)[:3]
        confidences = [round(0.45 + b[4] / 255 * 0.5, 2), 0.0, 0.0]
        rest = round((1 - confidences[0]) * 0.7, 2)
        confidences[1], confidences[2] = rest, round(1 - confidences[0] - rest, 2)
        fish.append({
            "bbox": [x1, y1, min(x2, width), min(y2, height)],
            "det_confidence": round(0.6 + b[5] / 255 * 0.39, 2),
            "class": CLASSES[top[0]],
            "class_confidence": confidences[0],
            "uncertain": confidences[0] < UNCERTAIN_BELOW,
            "top3": [{"label": CLASSES[c], "confidence": p} for c, p in zip(top, confidences)],
        })
    return fish


def layout_detections(image: bytes):
    """Boxes of the drawn fish, only for the sample picture; None for any other upload."""
    sample = HERE / "aquarium-synthetic.jpg"
    if not (USE_LAYOUT and sample.exists() and image == sample.read_bytes()):
        return None
    detections = []
    for i, fish in enumerate(json.loads((HERE / "aquarium-synthetic.layout.json").read_text())["fish"]):
        others = [c for c in CLASSES if c != fish["class"]]
        rest = round((1 - fish["class_confidence"]) / 2, 2)
        detections.append({
            "bbox": fish["bbox"],
            "det_confidence": round(0.95 - 0.03 * i, 2),
            "class": fish["class"],
            "class_confidence": fish["class_confidence"],
            "uncertain": fish["uncertain"],
            "top3": [{"label": fish["class"], "confidence": fish["class_confidence"]},
                     {"label": others[i % len(others)], "confidence": rest},
                     {"label": others[(i + 1) % len(others)], "confidence": round(1 - fish["class_confidence"] - rest, 2)}],
        })
    return detections


@app.post("/analyze")
async def analyze(request: Request):
    body = await request.json()
    filename = str(body.get("filename", "")).lower()
    image = base64.b64decode(body["image_bytes"])

    if "slow" in filename:
        await asyncio.sleep(DELAY_SECONDS)
    if "broken" in filename:
        raise HTTPException(status_code=500, detail="Detection failed: mock error")

    width, height = image_size(image)
    digest = hashlib.sha256(image).digest()
    detections = [] if "empty" in filename else (layout_detections(image) or canned_detections(digest, width, height))
    return {
        "detections": detections,
        "image": base64.b64encode(image).decode(),  # the real service returns the photo re-encoded as JPEG
        "image_width": width,
        "image_height": height,
    }


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("MOCK_HOST", "127.0.0.1"), port=int(os.getenv("MOCK_PORT", "8000")))
