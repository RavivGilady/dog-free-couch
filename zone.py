"""
Couch-zone geometry: is a detected dog box "on the couch"?

The couch zone is a polygon (usually a quadrilateral) the user draws once
with calibrate.py by clicking the couch's corners in the camera view.
"on the couch" is decided by how much of the dog's bounding box overlaps
that polygon, not just whether a single point is inside it -- that makes it
robust to a dog that's half on / half off the couch, or standing next to it
with its box merely brushing the edge.
"""
from __future__ import annotations

import numpy as np


def polygon_mask(points, shape) -> np.ndarray:
    """Rasterize a polygon (list of (x, y)) into a boolean mask of `shape` (h, w)."""
    import cv2

    mask = np.zeros(shape, dtype=np.uint8)
    if points and len(points) >= 3:
        pts = np.array(points, dtype=np.int32).reshape((-1, 1, 2))
        cv2.fillPoly(mask, [pts], 1)
    return mask.astype(bool)


def box_overlap_fraction(box, zone_mask: np.ndarray) -> float:
    """Fraction (0..1) of `box`'s area that falls inside `zone_mask`.

    box: (x1, y1, x2, y2) in the same pixel coordinates the mask was built in.
    """
    x1, y1, x2, y2 = box
    h, w = zone_mask.shape[:2]
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return 0.0

    box_area = (x2 - x1) * (y2 - y1)
    if box_area <= 0:
        return 0.0

    overlap = zone_mask[y1:y2, x1:x2]
    return float(overlap.sum()) / float(box_area)


def is_dog_on_couch(dog_box, zone_points, frame_shape, overlap_threshold: float = 0.35) -> bool:
    """True if `dog_box` overlaps the couch polygon by at least `overlap_threshold`."""
    if not zone_points or len(zone_points) < 3:
        # No zone calibrated yet -- nothing to compare against.
        return False
    mask = polygon_mask(zone_points, frame_shape[:2])
    return box_overlap_fraction(dog_box, mask) >= overlap_threshold
