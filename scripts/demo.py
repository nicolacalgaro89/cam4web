#!/usr/bin/env python3
"""Generate synthetic JPEGs and upload them through the real API."""

import argparse
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from cam4web_agent import upload

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default=os.getenv("CAM4WEB_URL", "http://127.0.0.1:8000"))
parser.add_argument("--camera", default=os.getenv("CAM4WEB_CAMERA", "casa"))
parser.add_argument("--hours", type=int, default=24)
parser.add_argument(
    "--output", type=Path, help="Also save the latest JPEG for testing the agent"
)
args = parser.parse_args()
if not 1 <= args.hours <= 24:
    parser.error("--hours must be between 1 and 24")
if not os.getenv("CAM4WEB_TOKEN"):
    parser.error("Set CAM4WEB_TOKEN to the camera upload token")
now = datetime.now(timezone.utc)
for age in reversed(range(args.hours)):
    captured = now - timedelta(hours=age)
    image = Image.new("RGB", (1280, 720), (20 + age * 3, 65 + age * 2, 85 + age * 3))
    draw = ImageDraw.Draw(image)
    draw.ellipse((850 - age * 12, 80, 1030 - age * 12, 260), fill=(245, 208, 127))
    draw.polygon(
        [
            (0, 580),
            (350, 240),
            (650, 600),
            (960, 350),
            (1280, 610),
            (1280, 720),
            (0, 720),
        ],
        fill=(28, 67, 55),
    )
    draw.rectangle((32, 28, 640, 110), fill=(15, 26, 23))
    draw.text(
        (48, 45), "cam4web | DEMO - IMMAGINE SINTETICA", fill="white", font_size=24
    )
    draw.text(
        (48, 78), captured.isoformat(timespec="seconds"), fill="white", font_size=18
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    upload(
        args.url,
        args.camera,
        os.environ["CAM4WEB_TOKEN"],
        buffer.getvalue(),
        captured.isoformat(),
    )
    print("Uploaded:", captured.isoformat(timespec="seconds"))
if args.output:
    args.output.write_bytes(buffer.getvalue())
