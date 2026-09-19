"""
Object detector: MobileNet-SSD (Caffe) via OpenCV's DNN module.

Why this model:
- No PyTorch/TensorFlow needed, just opencv-python -> tiny install, and it
  runs fast enough on CPU for both a laptop and a Raspberry Pi (unlike
  YOLOv8, which needs PyTorch and is much heavier on a Pi).
- Its label set (Pascal VOC) already includes exactly the two classes this
  project needs: "dog" and "sofa".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Pascal VOC classes, in the order MobileNet-SSD was trained to output them.
VOC_CLASSES = [
    "background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus",
    "car", "cat", "chair", "cow", "diningtable", "dog", "horse",
    "motorbike", "person", "pottedplant", "sheep", "sofa", "train",
    "tvmonitor",
]


@dataclass
class Detection:
    label: str
    confidence: float
    box: tuple  # (x1, y1, x2, y2) in pixel coordinates


class Detector:
    def __init__(self, prototxt_path: str, weights_path: str, confidence_threshold: float = 0.5):
        import cv2

        prototxt_path, weights_path = str(prototxt_path), str(weights_path)
        for p in (prototxt_path, weights_path):
            if not Path(p).exists():
                raise FileNotFoundError(
                    f"Model file not found: {p}\n"
                    "Run download_model.py first (see README) to fetch the "
                    "MobileNet-SSD weights."
                )

        self.net = cv2.dnn.readNetFromCaffe(prototxt_path, weights_path)
        self.confidence_threshold = confidence_threshold
        self._cv2 = cv2

    def detect(self, frame, wanted_labels=None) -> list:
        """Run detection on a single BGR frame.

        wanted_labels: optional iterable of label strings to keep (e.g.
        {"dog", "sofa"}); everything else is discarded before it's returned.
        """
        cv2 = self._cv2
        h, w = frame.shape[:2]

        blob = cv2.dnn.blobFromImage(
            cv2.resize(frame, (300, 300)), 0.007843, (300, 300), 127.5
        )
        self.net.setInput(blob)
        raw = self.net.forward()

        detections = []
        for i in range(raw.shape[2]):
            confidence = float(raw[0, 0, i, 2])
            if confidence < self.confidence_threshold:
                continue

            class_id = int(raw[0, 0, i, 1])
            if class_id < 0 or class_id >= len(VOC_CLASSES):
                continue
            label = VOC_CLASSES[class_id]

            if wanted_labels is not None and label not in wanted_labels:
                continue

            box = raw[0, 0, i, 3:7] * [w, h, w, h]
            x1, y1, x2, y2 = box.astype(int).tolist()
            # Clamp to frame bounds.
            x1, x2 = max(0, x1), min(w, x2)
            y1, y2 = max(0, y1), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                continue

            detections.append(Detection(label=label, confidence=confidence, box=(x1, y1, x2, y2)))

        return detections
