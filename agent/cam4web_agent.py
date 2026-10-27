#!/usr/bin/env python3
"""Raspberry agent: standard library only; camera capture uses rpicam-still."""

import argparse
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
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


class HourlyQueue:
    """Keep the first capture per UTC hour, including delivery markers across restarts."""

    def __init__(self, directory, url, camera):
        scope = hashlib.sha256((url.rstrip("/") + "/" + camera).encode()).hexdigest()[
            :24
        ]
        directory = Path(directory) / scope
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "queue.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS samples "
                "(hour INTEGER PRIMARY KEY, captured TEXT NOT NULL, stamp REAL NOT NULL, jpeg BLOB)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self, db, now):
        db.execute("DELETE FROM samples WHERE stamp<=?", (now - 86400,))
        db.execute(
            "DELETE FROM samples WHERE hour NOT IN "
            "(SELECT hour FROM samples ORDER BY hour DESC LIMIT 24)"
        )

    def retain(self, jpeg, captured_at):
        stamp = datetime.fromisoformat(captured_at).timestamp()
        with self.connect() as db:
            self.prune(db, stamp)
            db.execute(
                "INSERT OR IGNORE INTO samples VALUES (?,?,?,?)",
                (int(stamp // 3600), captured_at, stamp, jpeg),
            )
            self.prune(db, stamp)

    def acknowledge(self, captured_at):
        with self.connect() as db:
            # Preserve the marker so later captures in this hour do not refill the queue.
            db.execute("UPDATE samples SET jpeg=NULL WHERE captured=?", (captured_at,))

    def pending(self, now):
        with self.connect() as db:
            self.prune(db, now)
            return db.execute(
                "SELECT captured,jpeg FROM samples WHERE jpeg IS NOT NULL "
                "ORDER BY stamp LIMIT 4"
            ).fetchall()


def deliver(queue, url, camera, token, jpeg, captured_at):
    # Persist before networking, so a crash or outage cannot lose the hourly sample.
    queue.retain(jpeg, captured_at)
    upload(url, camera, token, jpeg, captured_at)
    queue.acknowledge(captured_at)
    logger.info("Current image uploaded (%d bytes)", len(jpeg))
    for timestamp, pending_jpeg in queue.pending(
        datetime.now(timezone.utc).timestamp()
    ):
        upload(url, camera, token, pending_jpeg, timestamp)
        queue.acknowledge(timestamp)
        logger.info("Hourly image recovered (%s)", timestamp)


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
    queue = HourlyQueue(os.getenv("CAM4WEB_QUEUE_DIR", "./agent-data"), url, camera)
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
            deliver(queue, url, camera, token, jpeg, captured)
        except (OSError, URLError, subprocess.SubprocessError) as error:
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
        # Keep capturing at the configured cadence even when uploads fail.
        time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
