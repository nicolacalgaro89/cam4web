#!/usr/bin/env python3
"""Raspberry agent: standard library only; camera capture uses rpicam-still."""

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def upload(url, camera, token, jpeg, captured_at):
    endpoint = url.rstrip("/") + "/api/v1/cameras/" + quote(camera, safe="") + "/images"
    request = Request(
        endpoint,
        data=jpeg,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "image/jpeg",
            "X-Captured-At": captured_at,
        },
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--image", type=Path, help="Upload a test JPEG instead of capturing the camera"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    url = os.environ["CAM4WEB_URL"]
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        parser.error("Use HTTPS, except for localhost testing")
    camera = os.getenv("CAM4WEB_CAMERA", "casa")
    token = os.environ["CAM4WEB_TOKEN"]
    interval = float(os.getenv("CAM4WEB_INTERVAL", "60"))
    width = int(os.getenv("CAM4WEB_WIDTH", "1280"))
    height = int(os.getenv("CAM4WEB_HEIGHT", "720"))
    if interval < 1 or width < 1 or height < 1:
        parser.error(
            "Interval, width and height must be positive (interval >= 1 second)"
        )
    command = shutil.which("rpicam-still") or shutil.which("libcamera-still")
    if not args.image and not command:
        parser.error("Install rpicam-apps-lite or provide --image test.jpg")
    failures = 0
    while True:
        started = time.monotonic()
        try:
            if args.image:
                jpeg = args.image.read_bytes()
            else:
                with tempfile.TemporaryDirectory(prefix="cam4web-") as temporary:
                    path = Path(temporary) / "capture.jpg"
                    subprocess.run(
                        [
                            command,
                            "--nopreview",
                            "--timeout",
                            "2000",
                            "--width",
                            str(width),
                            "--height",
                            str(height),
                            "--quality",
                            "85",
                            "--output",
                            str(path),
                        ],
                        check=True,
                        timeout=45,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    jpeg = path.read_bytes()
            captured = datetime.now(timezone.utc).isoformat()
            upload(url, camera, token, jpeg, captured)
            failures = 0
            logger.info("Image uploaded (%d bytes)", len(jpeg))
        except (OSError, URLError, subprocess.SubprocessError) as error:
            failures += 1
            # Do not log request headers or token-bearing URLs.
            logger.error(
                "Capture/upload failed (%s%s)",
                type(error).__name__,
                " HTTP " + str(error.code) if isinstance(error, HTTPError) else "",
            )
            if args.once:
                raise SystemExit(1)
        if args.once:
            return
        delay = max(interval, min(300, 2 ** min(failures, 8))) if failures else interval
        time.sleep(max(0, delay - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
