import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zone import box_overlap_fraction, is_dog_on_couch, polygon_mask


COUCH = [(100, 100), (300, 100), (300, 250), (100, 250)]  # 200x150 rectangle
FRAME_SHAPE = (480, 640, 3)


def test_box_fully_inside_zone_is_full_overlap():
    mask = polygon_mask(COUCH, FRAME_SHAPE[:2])
    frac = box_overlap_fraction((120, 120, 200, 200), mask)
    assert frac > 0.98


def test_box_fully_outside_zone_is_zero_overlap():
    mask = polygon_mask(COUCH, FRAME_SHAPE[:2])
    frac = box_overlap_fraction((400, 400, 450, 450), mask)
    assert frac == 0.0


def test_box_half_overlapping_zone_is_roughly_half():
    # Box straddles the right edge of the couch (x=300): half in, half out.
    mask = polygon_mask(COUCH, FRAME_SHAPE[:2])
    frac = box_overlap_fraction((250, 120, 350, 200), mask)
    assert 0.4 < frac < 0.6


def test_is_dog_on_couch_respects_threshold():
    dog_on_couch_box = (120, 120, 200, 200)  # fully inside
    dog_off_couch_box = (400, 400, 450, 450)  # fully outside
    dog_edge_box = (280, 120, 320, 200)  # small box, mostly inside

    assert is_dog_on_couch(dog_on_couch_box, COUCH, FRAME_SHAPE, overlap_threshold=0.35) is True
    assert is_dog_on_couch(dog_off_couch_box, COUCH, FRAME_SHAPE, overlap_threshold=0.35) is False
    assert is_dog_on_couch(dog_edge_box, COUCH, FRAME_SHAPE, overlap_threshold=0.9) is False


def test_no_zone_defined_never_triggers():
    assert is_dog_on_couch((120, 120, 200, 200), [], FRAME_SHAPE, overlap_threshold=0.1) is False
    assert is_dog_on_couch((120, 120, 200, 200), [(1, 1), (2, 2)], FRAME_SHAPE, 0.1) is False


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
