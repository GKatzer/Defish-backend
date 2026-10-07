# API reference

Seven routes, all unauthenticated; FastAPI serves interactive documentation at `/docs` and the schema at `/openapi.json` (the route list below matches it, see [examples/transcripts/error-cases.txt](examples/transcripts/error-cases.txt)).
Examples were run against the Docker stack with the **mock** inference service: classes and confidences are canned, the file names `empty`, `broken` and `slow` are the mock's switches. The base URL in the examples is `http://127.0.0.1:8001`;
the complete session is in [examples/transcripts/walkthrough.txt](examples/transcripts/walkthrough.txt) and can be replayed with [`examples/walkthrough.sh`](examples/walkthrough.sh).

| Route | Purpose |
|---|---|
| [`POST /analyze`](#post-analyze) | upload a photo: a task id, or the finished result if this exact photo was analysed before |
| [`GET /analyze-result/{task_id}`](#get-analyze-resulttask_id) | poll a task |
| [`POST /cancel/{task_id}`](#post-canceltask_id) | cancel a task |
| [`GET /handshake`](#get-handshake) | ask the inference service whether it is healthy |
| [`GET /cache-stats`](#get-cache-stats) | number of cached analyses and Redis memory |
| [`GET /clear-cache`](#get-clear-cache) | delete all cached analyses |
| [`GET /`](#get-) | liveness |

CORS allows every origin, method and header. Error bodies are FastAPI's `{"detail": ...}`.

## `POST /analyze`

`multipart/form-data` with one file field named `image`. Any file up to `MAX_UPLOAD_MB` (default 10) is accepted; the API does not check that it is an image.

```bash
curl -s -F image=@aquarium-synthetic.jpg http://127.0.0.1:8001/analyze
```
```json
{"task_id":"857ef143-ed65-4a1d-aa2b-b469330cc91a","cached":false,"message":"Task submitted successfully","result":null}
```

If the same bytes were analysed within the cache lifetime, the answer is the finished analysis itself (a [result object](#result-object), no `task_id`) and the upload is still logged as an analysis row:

```bash
curl -s -F image=@aquarium-synthetic.jpg http://127.0.0.1:8001/analyze | jq -c '{id, diagnosis, confidence, detections: (.detections | length), task_id}'
```
```json
{"id":"85","diagnosis":"mycobacteriosis, hexamitosis","confidence":0.735,"detections":2,"task_id":null}
```

If the cached analysis has lost its photo (the photo lives one hour in its own Redis key and can be evicted), the upload is analysed again and the answer is a task id.

A client distinguishes the two answers by the presence of `task_id` (the reference web client tests `id` and `diagnosis` first).

| Status | When |
|---|---|
| 200 | task accepted, or cache hit |
| 413 | the file is larger than `MAX_UPLOAD_MB`: `{"detail":"Image is larger than 10 MB"}` (nothing is queued, no user row is created) |
| 422 | no `image` field: `{"detail":[{"type":"missing","loc":["body","image"],"msg":"Field required","input":null}]}` |
| 500 | `{"detail":"Task queue error"}` (RabbitMQ unreachable or publishing failed), `{"detail":"couldnt get task_id"}`, `{"detail":"Internal server error"}` (anything else, for example the database) |

The client's address is taken from `X-Forwarded-For` (first entry), then `X-Real-IP`, then the connection, and stored in `users`.

## `GET /analyze-result/{task_id}`

Always `200`. The order of checks is fixed: cancel flag, error, result, otherwise still processing. An id that was never issued is indistinguishable from a task that is still running.

```json
{"status":"processing","message":"Image analysis is still in progress","task_id":"857ef143-ed65-4a1d-aa2b-b469330cc91a"}
```
```json
{"status":"failed","message":"ML service returned HTTP 500","task_id":"a9d31efd-8f20-48ed-b2c4-824c722b2710"}
```
```json
{"status":"canceled","message":"Analysis canceled by user","task_id":"e8eafb5c-0857-438d-b773-1960c134440a"}
```

`failed` messages are one of `ML service unavailable`, `ML service timed out`, `ML service returned HTTP <n>`, `Internal error`. When the task is done the body is the result object, which has no `status` field.
Results are kept for one hour; after that (or after Redis evicted them) the same id reads as `processing` again.

### Result object

```bash
curl -s http://127.0.0.1:8001/analyze-result/857ef143-ed65-4a1d-aa2b-b469330cc91a | jq 'if has("original_image") then .original_image |= (.[0:20] + "...") else . end'
```
```json
{
  "id": "84",
  "diagnosis": "mycobacteriosis, hexamitosis",
  "confidence": 0.735,
  "recommendations": "Mycobacteriosis (fish tuberculosis). A chronic disease that is hard to cure. Recommendations: isolate the sick fish; ...",
  "original_image": "/9j/4AAQSkZJRgABAQAA...",
  "image_format": "jpeg",
  "image_width": 640,
  "image_height": 480,
  "detections": [
    {
      "x_min": 253, "y_min": 55, "x_max": 417, "y_max": 128,
      "detection_class": "0",
      "detection_confidence": 0.62,
      "classification_class": "mycobacteriosis",
      "classification_confidence": 0.58,
      "recommendations": "Mycobacteriosis (fish tuberculosis). ...",
      "uncertain": true,
      "top3": [{"label": "mycobacteriosis", "confidence": 0.58}, {"label": "oodiniosis", "confidence": 0.29}, {"label": "healthy", "confidence": 0.13}]
    },
    { "...": "one object per detected fish" }
  ]
}
```
(shortened; the full output is in the transcript.)

| Field | Meaning |
|---|---|
| `id` | id of the row in `fish_analyses`, as a string |
| `diagnosis` | the class of every detected fish joined with `", "`, in the order of the detections (so `healthy, healthy` is possible); `No objects detected` when there are none |
| `confidence` | mean of the classification confidences of all detections; `0.0` without detections |
| `recommendations` | care advice for the whole photo: one text per distinct class, diseases first, the `healthy` text only if no fish is sick; a "no fish found, take a clearer photo" text when nothing was detected |
| `original_image` | the photo as base64 JPEG, as returned by the inference service (the uploaded photo if the service sent none), read from its own Redis key; `null` when the photo has expired or been evicted, the rest of the result is still delivered |
| `image_format`, `image_width`, `image_height` | `jpeg` and the size in pixels of the photo that the boxes refer to |
| `detections[]` | one entry per fish, below |

Detection fields: box corners in pixels of the original photo; `detection_class` is always `"0"` (the inference service sends no detector class); `detection_confidence` is the detector's score (`det_confidence` in the service's answer);
`classification_class` is one of `healthy`, `fin_rot`, `dermatomycosis`, `hexamitosis`, `mycobacteriosis`, `oodiniosis`, `plistophorosis`; `uncertain` is the inference service's confidence gate (below its threshold the label is a weak hint);
`top3` the three most probable classes; `recommendations` the advice for this fish. The advice is general guidance, not veterinary advice. A class the API does not know gets `The diagnosis was not recognised. It is recommended to consult a fish pathologist ...`.

When no fish is found:

```json
{"id": "86", "diagnosis": "No objects detected", "confidence": 0.0, "recommendations": "No fish was found in the photo. Take a sharper picture in good light so that the whole fish is visible.", "image_format": "jpeg", "image_width": 640, "image_height": 480, "detections": [], "original_image": "..."}
```

## `POST /cancel/{task_id}`

Sets a flag for one hour and always answers `{"status":"canceled"}`, also for an id that does not exist. The poll endpoint then reports `canceled`. A worker that has not saved anything yet stops; one that is waiting for the inference service finishes the wait and discards the result.

```bash
curl -s -X POST http://127.0.0.1:8001/cancel/e8eafb5c-0857-438d-b773-1960c134440a
```
```json
{"status":"canceled"}
```

## `GET /handshake`

Calls `GET {ML_SERVER_URL}/health` (5 s timeout) and returns the answer as a string. When the service cannot be reached the status code is still 200:

```json
{"response":"{\"status\":\"healthy\",\"service\":\"ml-inference\",\"version\":\"1.0.0\",\"detector\":\"loaded\",\"classifier\":\"loaded\",\"mock\":true}"}
```
```json
{"error":"ML service unreachable"}
```

## `GET /cache-stats`

Counts the keys `fish:*` (with `KEYS`, which blocks Redis while it runs) and shows up to three of them without the photos. `{"status":"Redis not connected"}` when there is no Redis, `{"status":"error"}` on a failure.

```json
{"status":"connected","total_cached_images":2,"memory_used":"26.68M","example_cached_items":[{"diagnosis":"mycobacteriosis, hexamitosis","cached_at":"2026-10-04T16:55:14.419218"},{"diagnosis":"No objects detected","cached_at":"2026-10-04T16:55:14.472499"}]}
```

`memory_used` is Redis' total, not only the cache. Each item also has a `key` (`fish:<md5>`), left out of the example.

## `GET /clear-cache`

Deletes every `fish:*` key and answers `{"status":"success","cleared":2}`. It is a `GET`, has side effects and is not protected: anyone who can reach the API can empty the cache. Results of running tasks (`result:*`) are not touched.

## `GET /`

```json
{"message": "Defish API is running"}
```

## Calls the API makes to the inference service

For orientation, the contract the worker relies on (the real service is [`Defish-inference`](https://github.com/GKatzer/Defish-inference); the field names were read from its source, and no live run against it was made for this documentation):

- `GET {ML_SERVER_URL}/health`
- `POST {ML_SERVER_URL}/analyze` with `{"image_bytes": "<base64>", "filename": "...", "content_type": "...", "user_id": 1}`; the service reads only `image_bytes`. It answers
  `{"detections": [{"bbox": [x1, y1, x2, y2], "det_confidence", "class", "class_confidence", "uncertain", "top3"}], "image": "<base64 JPEG>", "image_width", "image_height"}`; any status other than 200 fails the task.
