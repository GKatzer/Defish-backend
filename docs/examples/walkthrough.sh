#!/usr/bin/env bash
# Walks through the API against a running stack and prints every command with its answer.
# Needs curl and jq. The stack is expected to use the mock inference service (docker-compose.mock-ml.yml):
# the file names "empty", "broken" and "slow" are its switches.
#
#   docs/examples/walkthrough.sh [base-url]        # default http://127.0.0.1:8001
set -u
BASE=${1:-http://127.0.0.1:8001}
cd "$(dirname "$0")" || exit 1
IMG=aquarium-synthetic.jpg
SHORT='if has("original_image") then .original_image |= (.[0:20] + "...") else . end'   # keep the output readable

run() { echo; echo "\$ $1"; eval "$1"; }
variant() {    # a copy of the photo with one extra byte: another hash, so the cache does not answer for it
  local f; f=$(mktemp --suffix=.jpg); cat "$IMG" > "$f"; printf '%s' "$1" >> "$f"; echo "$f"
}
wait_for() {   # poll until the task is no longer "processing" (gives up after 60 s)
  for _ in $(seq 60); do
    [ "$(curl -s "$BASE/analyze-result/$1" | jq -r '.status // "done"')" != processing ] && return
    sleep 1
  done
  echo "task $1 still processing after 60 s" >&2
}

echo "## demo reset: forget cached analyses"
run "curl -s $BASE/clear-cache"

echo; echo "## 1. connectivity to the inference service"
run "curl -s $BASE/handshake"

echo; echo "## 2. upload a new photo: the answer is a task id"
echo; echo "\$ curl -s -F image=@aquarium-synthetic.jpg $BASE/analyze"
SUBMITTED=$(curl -s -F "image=@$IMG" "$BASE/analyze"); echo "$SUBMITTED"
TASK=$(jq -r .task_id <<<"$SUBMITTED")
run "curl -s $BASE/analyze-result/$TASK | jq -c '.status // \"done\"'   # processing until a worker is done"
wait_for "$TASK"
run "curl -s $BASE/analyze-result/$TASK | jq '$SHORT'"

echo; echo "## 3. the same photo again: answered from the Redis cache (keyed by the image bytes), no task"
run "curl -s -F image=@$IMG $BASE/analyze | jq -c '{id, diagnosis, confidence, detections: (.detections | length), task_id}'"

echo; echo "## 4. no fish found (mock switch: file name contains 'empty')"
TASK=$(curl -s -F "image=@$(variant empty);filename=empty.jpg" "$BASE/analyze" | jq -r .task_id)
wait_for "$TASK"
run "curl -s $BASE/analyze-result/$TASK | jq '$SHORT'"

echo; echo "## 5. the inference service fails (mock switch: 'broken')"
TASK=$(curl -s -F "image=@$(variant broken);filename=broken.jpg" "$BASE/analyze" | jq -r .task_id)
wait_for "$TASK"
run "curl -s $BASE/analyze-result/$TASK"

echo; echo "## 6. cancel a slow analysis (mock switch: 'slow', 8 s)"
TASK=$(curl -s -F "image=@$(variant slow);filename=slow.jpg" "$BASE/analyze" | jq -r .task_id)
run "curl -s -X POST $BASE/cancel/$TASK"
run "curl -s $BASE/analyze-result/$TASK"

echo; echo "## 7. a task id nobody issued"
run "curl -s $BASE/analyze-result/00000000-0000-0000-0000-000000000000"

echo; echo "## 8. cache statistics and interactive docs"
run "curl -s $BASE/cache-stats | jq -c 'del(.example_cached_items[].key)'"
run "curl -s -o /dev/null -w '%{http_code}\n' $BASE/docs"
