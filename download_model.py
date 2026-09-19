"""
Fetches the MobileNet-SSD (Caffe) model files used by detector.py.

These two small files (~28MB total) aren't bundled in this project to keep
it lightweight; run this once after installing requirements.txt.

Usage:
    python download_model.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

FILES = {
    "MobileNetSSD_deploy.prototxt": (
        "https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/"
        "MobileNetSSD_deploy.prototxt"
    ),
    "MobileNetSSD_deploy.caffemodel": (
        "https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/"
        "MobileNetSSD_deploy.caffemodel"
    ),
}


def main():
    out_dir = Path(__file__).parent / "models"
    out_dir.mkdir(exist_ok=True)

    for filename, url in FILES.items():
        dest = out_dir / filename
        if dest.exists() and dest.stat().st_size > 0:
            print(f"Already have {filename}, skipping.")
            continue
        print(f"Downloading {filename} ...")
        try:
            urllib.request.urlretrieve(url, dest)
            print(f"  -> saved to {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            print(
                "  You can also download it manually from "
                "https://github.com/chuanqi305/MobileNet-SSD and place both "
                f"files in {out_dir}/",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
