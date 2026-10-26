import asyncio
import hmac
import io
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

UTC = timezone.utc
STATIC = Path(__file__).parent / "static"
Image.MAX_IMAGE_PIXELS = 20_000_000


def create_app(data_dir=None, cameras=None):
    directory = Path(data_dir or os.getenv("DATA_DIR", "./data"))
    configured = (
        cameras if cameras is not None else json.loads(os.getenv("CAMERA_TOKENS", "{}"))
    )
    if not isinstance(configured, dict) or not configured:
        raise ValueError(
            "Configure CAMERA_TOKENS as a JSON object mapping camera IDs to secret tokens"
        )
    for camera, token in configured.items():
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", camera)
            or not isinstance(token, str)
            or len(token) < 24
        ):
            raise ValueError(
                "Camera IDs must be URL-safe; tokens must contain at least 24 characters"
            )
    max_bytes = int(os.getenv("MAX_IMAGE_BYTES", "5242880"))
    if max_bytes <= 0:
        raise ValueError("MAX_IMAGE_BYTES must be positive")
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "cam4web.sqlite3"

    def connect():
        db = sqlite3.connect(db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS images (
            id TEXT PRIMARY KEY, camera TEXT NOT NULL, captured REAL NOT NULL,
            received REAL NOT NULL, hour INTEGER NOT NULL, hourly INTEGER NOT NULL,
            jpeg BLOB NOT NULL)""")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS hourly_image ON images(camera,hour) WHERE hourly=1"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS camera_time ON images(camera,captured DESC)"
        )

    def prune(db, now):
        db.execute("DELETE FROM images WHERE captured < ?", (now - 86400,))
        db.execute("""UPDATE images SET hourly=0 WHERE hourly=1 AND id NOT IN
            (SELECT id FROM (SELECT id, ROW_NUMBER() OVER
             (PARTITION BY camera ORDER BY captured DESC) AS rank FROM images WHERE hourly=1)
             WHERE rank<=24)""")
        db.execute("""DELETE FROM images WHERE hourly=0 AND id NOT IN
            (SELECT id FROM images i WHERE id=(SELECT id FROM images j
             WHERE j.camera=i.camera ORDER BY captured DESC, received DESC LIMIT 1))""")

    async def cleanup():
        while True:
            with connect() as db:
                prune(db, datetime.now(UTC).timestamp())
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(cleanup())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="cam4web", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def camera_exists(camera_id: str):
        if camera_id not in configured:
            raise HTTPException(404, "Camera non trovata")
        return camera_id

    # Public read policy: replace this dependency when adding user accounts.
    def can_view(camera_id: str = Depends(camera_exists)):
        return camera_id

    def can_upload(request: Request, camera_id: str = Depends(camera_exists)):
        expected = "Bearer " + configured[camera_id]
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise HTTPException(
                401, "Token non valido", headers={"WWW-Authenticate": "Bearer"}
            )
        return camera_id

    def metadata(row):
        return {
            "id": row["id"],
            "captured_at": datetime.fromtimestamp(row["captured"], UTC).isoformat(),
            "received_at": datetime.fromtimestamp(row["received"], UTC).isoformat(),
            "url": f"/api/v1/cameras/{row['camera']}/images/{row['id']}",
        }

    def image_response(row, download=False):
        if row is None:
            raise HTTPException(404, "Immagine non disponibile")
        disposition = "attachment" if download else "inline"
        return Response(
            bytes(row["jpeg"]),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'{disposition}; filename="{row["camera"]}-{row["id"]}.jpg"',
            },
        )

    @app.get("/")
    def index():
        return FileResponse(
            STATIC / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/health")
    def health():
        with connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/api/v1/cameras")
    def list_cameras():
        return {"cameras": [{"id": camera} for camera in configured]}

    @app.post("/api/v1/cameras/{camera_id}/images", status_code=201)
    async def upload(request: Request, camera_id: str = Depends(can_upload)):
        if request.headers.get("content-type", "").split(";")[0] != "image/jpeg":
            raise HTTPException(415, "Inviare un JPEG con Content-Type: image/jpeg")
        raw_time = request.headers.get("x-captured-at", "")
        try:
            captured = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if captured.utcoffset() is None:
                raise ValueError()
            stamp = captured.timestamp()
        except (ValueError, OverflowError):
            raise HTTPException(
                422, "X-Captured-At deve essere una data ISO 8601 con fuso orario"
            )
        now = datetime.now(UTC).timestamp()
        if stamp < now - 86400 or stamp > now + 300:
            raise HTTPException(
                422,
                "Scatto fuori intervallo: ultime 24 ore, massimo 5 minuti nel futuro",
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise HTTPException(413, "Immagine troppo grande")
        try:
            with Image.open(io.BytesIO(body)) as img:
                if img.format != "JPEG" or img.width * img.height > 20_000_000:
                    raise ValueError()
                img.load()
                # Re-encode to remove EXIF/GPS and normalize incoming files.
                normalized = io.BytesIO()
                img.convert("RGB").save(normalized, format="JPEG", quality=88)
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise HTTPException(422, "JPEG non valido o dimensioni eccessive")
        image_id = uuid4().hex
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prune(db, now)
            hour = int(stamp // 3600)
            hourly = not db.execute(
                "SELECT 1 FROM images WHERE camera=? AND hour=? AND hourly=1",
                (camera_id, hour),
            ).fetchone()
            db.execute(
                "INSERT INTO images VALUES (?,?,?,?,?,?,?)",
                (
                    image_id,
                    camera_id,
                    stamp,
                    now,
                    hour,
                    int(hourly),
                    normalized.getvalue(),
                ),
            )
            row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            prune(db, now)
        return metadata(row)

    @app.get("/api/v1/cameras/{camera_id}/status")
    def status(camera_id: str = Depends(can_view)):
        with connect() as db:
            row = db.execute(
                "SELECT * FROM images WHERE camera=? AND captured>=? ORDER BY captured DESC,received DESC LIMIT 1",
                (camera_id, datetime.now(UTC).timestamp() - 86400),
            ).fetchone()
        return {"latest": metadata(row) if row else None}

    @app.get("/api/v1/cameras/{camera_id}/latest")
    def latest(download: bool = False, camera_id: str = Depends(can_view)):
        with connect() as db:
            row = db.execute(
                "SELECT * FROM images WHERE camera=? AND captured>=? ORDER BY captured DESC,received DESC LIMIT 1",
                (camera_id, datetime.now(UTC).timestamp() - 86400),
            ).fetchone()
        return image_response(row, download)

    @app.get("/api/v1/cameras/{camera_id}/history")
    def history(camera_id: str = Depends(can_view)):
        with connect() as db:
            rows = db.execute(
                "SELECT id,camera,captured,received FROM images WHERE camera=? AND hourly=1 AND captured>=? ORDER BY captured DESC LIMIT 24",
                (camera_id, datetime.now(UTC).timestamp() - 86400),
            ).fetchall()
        return {"images": [metadata(row) for row in rows]}

    @app.get("/api/v1/cameras/{camera_id}/images/{image_id}")
    def get_image(
        image_id: str, download: bool = False, camera_id: str = Depends(can_view)
    ):
        with connect() as db:
            row = db.execute(
                "SELECT * FROM images WHERE id=? AND camera=? AND captured>=?",
                (image_id, camera_id, datetime.now(UTC).timestamp() - 86400),
            ).fetchone()
        return image_response(row, download)

    return app
