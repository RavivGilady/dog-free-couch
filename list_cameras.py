"""
Pick which camera the monitor uses.

Finds every camera attached to this computer, names them (so you can tell
the real webcam from the infrared Windows Hello sensor or a meeting app's
virtual camera), shows you each one's picture, and writes the one you choose
into config.yaml -- so nothing has to be guessed and hand-edited.

Usage:
    python list_cameras.py                  # find, preview, choose, save
    python list_cameras.py --list           # just list what was found
    python list_cameras.py --index 1 --save # save a known choice, no window

Controls in the preview window:
    n / right    next camera
    p / left     previous camera
    s / enter    use this camera (writes config.yaml)
    q / esc      quit without changing anything
"""
from __future__ import annotations

import argparse
import sys

import cameras


def _print_found(found: list[cameras.Camera]) -> None:
    if not found:
        print("\nNo cameras found.")
        print("On Windows, check Settings > Privacy & security > Camera, and "
              "close anything else using the camera (Teams, Zoom, Camera app).")
        return

    print(f"\nFound {len(found)} camera(s):\n")
    for n, cam in enumerate(found, 1):
        print(f"  {n}. {cam.describe()}")
    if not cameras.names_are_authoritative():
        print("\nNote: the names come from the OS device list and may not line "
              "up with the indices -- trust the picture, not the name.")
        if sys.platform == "win32":
            print("`pip install pygrabber` makes the names exact on Windows.")


def _choose_with_preview(found: list[cameras.Camera], config_path: str) -> int:
    """Show a live preview and let the user step through and pick. Returns an
    exit code; falls back to the text prompt if no window can be opened."""
    import cv2
    import numpy as np

    current = found.index(cameras.pick_default(found))
    window = "Pick a camera -- 'n' next, 'p' previous, 's' use this one, 'q' quit"

    try:
        cv2.namedWindow(window)
    except cv2.error:
        print("No display available for the preview window.", file=sys.stderr)
        return _choose_with_prompt(found, config_path)

    cap = None

    def open_current():
        cam = found[current]
        flag = {"any": cv2.CAP_ANY,
                "dshow": getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY),
                "msmf": getattr(cv2, "CAP_MSMF", cv2.CAP_ANY)}.get(cam.backend, cv2.CAP_ANY)
        print(f"Showing {current + 1}/{len(found)}: {cam.describe()}")
        c = cv2.VideoCapture(cam.index, flag)
        return c if c.isOpened() else None

    cap = open_current()
    try:
        while True:
            frame = None
            if cap is not None:
                ok, frame = cap.read()
                if not ok:
                    frame = None
            if frame is None:
                frame = np.zeros((240, 640, 3), dtype="uint8")
                cv2.putText(frame, "No picture from this camera", (10, 130),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

            cam = found[current]
            cv2.putText(frame, f"{current + 1}/{len(found)}  {cam.label}", (10, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(frame, f"index={cam.index} backend={cam.backend}  "
                               "[s] use  [n] next  [q] quit",
                        (10, frame.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 255, 0), 1)
            cv2.imshow(window, frame)

            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                print("Nothing changed.")
                return 0
            if key in (ord("n"), 83, ord("p"), 81):
                step = 1 if key in (ord("n"), 83) else -1
                if cap is not None:
                    cap.release()
                current = (current + step) % len(found)
                cap = open_current()
            elif key in (ord("s"), 13):
                cameras.save_choice(config_path, cam.index, cam.backend)
                print(f"\nSaved: {cam.label} (index={cam.index}, "
                      f"backend_api={cam.backend}) -> {config_path}")
                print("Run `python calibrate.py` next if the camera moved.")
                return 0
    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()


def _choose_with_prompt(found: list[cameras.Camera], config_path: str) -> int:
    """Text-only choice, for a headless box (a Pi over SSH, say)."""
    if not sys.stdin.isatty():
        print("\nNot a terminal, so nothing was saved. Re-run with "
              "--index N --save to pick one.")
        return 0
    try:
        answer = input(f"\nWhich camera? [1-{len(found)}, or blank to cancel] ").strip()
    except EOFError:
        return 0
    if not answer.isdigit() or not 1 <= int(answer) <= len(found):
        print("Nothing changed.")
        return 0
    cam = found[int(answer) - 1]
    cameras.save_choice(config_path, cam.index, cam.backend)
    print(f"Saved: {cam.label} (index={cam.index}, backend_api={cam.backend}) "
          f"-> {config_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Find your cameras and save the one to use into config.yaml.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--list", action="store_true",
                    help="only list the cameras found, then exit")
    ap.add_argument("--no-preview", action="store_true",
                    help="choose from the text list instead of a preview window")
    ap.add_argument("--index", type=int,
                    help="save this index without probing or previewing")
    ap.add_argument("--backend", default=None,
                    help="backend for --index: dshow, msmf or any")
    ap.add_argument("--save", action="store_true",
                    help="with --index, write the choice to config.yaml")
    ap.add_argument("--max-index", type=int, default=cameras.MAX_INDEX,
                    help=f"highest index to probe (default {cameras.MAX_INDEX})")
    args = ap.parse_args(argv)

    if args.index is not None:
        backend = args.backend or cameras.backends_for()[0]
        if not args.save:
            cam = cameras.probe(args.index, backend)
            print(cam.describe())
            print("Add --save to write this into config.yaml.")
            return 0 if cam.live else 1
        cameras.save_choice(args.config, args.index, backend)
        print(f"Saved index={args.index}, backend_api={backend} -> {args.config}")
        return 0

    print("Looking for cameras (this takes a few seconds)...", flush=True)
    found = cameras.discover(
        max_index=args.max_index,
        on_progress=lambda i, b: print(f"  trying index={i} backend={b}...", flush=True),
    )
    _print_found(found)

    if not found or args.list:
        return 0 if found else 1
    if args.no_preview:
        return _choose_with_prompt(found, args.config)
    return _choose_with_preview(found, args.config)


if __name__ == "__main__":
    sys.exit(main())
