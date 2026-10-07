"""Draws a synthetic aquarium picture (gradient, plants, a few fish-like ellipses) for the examples.

The picture is generated from scratch with a fixed seed: it contains no third-party content and is not a
photo, so no detector can be judged on it. Needs Pillow:

    python docs/examples/make_test_image.py [out.jpg] [--layout fish.json]

--layout also writes where the four fish were drawn (box corners in pixels) with an arbitrary class for each,
which the mock inference service uses when started with MOCK_USE_LAYOUT=1, so that its boxes sit on the fish.
"""
import json
import random
import sys

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 640, 480
rng = random.Random(7)

image = Image.new("RGB", (WIDTH, HEIGHT))
draw = ImageDraw.Draw(image)
for y in range(HEIGHT):  # water: light at the top, dark at the bottom
    t = y / HEIGHT
    draw.line([(0, y), (WIDTH, y)], fill=(int(30 - 20 * t), int(120 - 70 * t), int(170 - 80 * t)))
draw.rectangle([0, HEIGHT - 40, WIDTH, HEIGHT], fill=(176, 154, 110))  # sand

for x in range(30, WIDTH, 90):  # plants
    for k in range(3):
        draw.line([(x + k * 8, HEIGHT - 40), (x + k * 8 + rng.randint(-15, 15), HEIGHT - rng.randint(120, 220))],
                  fill=(30, rng.randint(110, 160), 60), width=5)

layout = []
CANNED = [("healthy", 0.93, False), ("fin_rot", 0.88, False), ("oodiniosis", 0.62, True), ("healthy", 0.91, False)]
for number in range(4):  # "fish": body ellipse, tail triangle, eye
    x, y = rng.randint(60, WIDTH - 160), rng.randint(60, HEIGHT - 200)
    w, h = rng.randint(70, 110), rng.randint(32, 48)
    colour = (rng.randint(200, 255), rng.randint(90, 170), rng.randint(20, 80))
    draw.ellipse([x, y, x + w, y + h], fill=colour)
    draw.polygon([(x + w - 4, y + h // 2), (x + w + 28, y - 8), (x + w + 28, y + h + 8)], fill=colour)
    draw.ellipse([x + 12, y + h // 3 - 3, x + 20, y + h // 3 + 5], fill=(15, 15, 15))
    label, confidence, uncertain = CANNED[number]
    layout.append({"bbox": [x, y - 8, x + w + 28, y + h + 8], "class": label,
                   "class_confidence": confidence, "uncertain": uncertain})

args = [a for a in sys.argv[1:] if not a.startswith("--")]
image.save(args[0] if args else "aquarium-synthetic.jpg", quality=85)
if "--layout" in sys.argv:
    path = sys.argv[sys.argv.index("--layout") + 1]
    args = [a for a in args if a != path]
    json.dump({"width": WIDTH, "height": HEIGHT, "fish": layout}, open(path, "w"), indent=1)
