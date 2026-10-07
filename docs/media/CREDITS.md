# Media and example inputs

This repository contains no photographs and no third-party images.

| File | What it is | Source and licence |
|---|---|---|
| [`../examples/aquarium-synthetic.jpg`](../examples/aquarium-synthetic.jpg) | 640x480 picture of a gradient, plants and four fish-like ellipses, used as the example upload | Drawn from scratch by [`../examples/make_test_image.py`](../examples/make_test_image.py) with a fixed seed (Pillow). No external content; released under the repository's MIT licence. It is not a photo of a fish, so nothing can be concluded from how a detector treats it. |

The example outputs in [`../examples/transcripts/`](../examples/transcripts/) come from the mock inference service
([`../examples/mock_ml_server.py`](../examples/mock_ml_server.py)): classes and confidences in them are canned, not model predictions.
