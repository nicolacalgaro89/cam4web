import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server.main import create_app

TOKEN = "a-test-secret-with-at-least-24-characters"


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path, {"casa": TOKEN, "altra": TOKEN + "2"})
    ) as value:
        value.db = tmp_path / "cam4web.sqlite3"
        yield value


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(output, "JPEG")
    return output.getvalue()


def send(client, stamp=None, body=None, token=TOKEN, camera="casa"):
    return client.post(
        f"/api/v1/cameras/{camera}/images",
        content=jpeg() if body is None else body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "image/jpeg",
            "X-Captured-At": stamp or datetime.now(timezone.utc).isoformat(),
        },
    )


def test_auth_and_validation(client, monkeypatch, tmp_path):
    assert send(client, token="wrong").status_code == 401
    assert send(client, body=b"not jpeg").status_code == 422
    assert send(client, stamp="2020-01-01T00:00:00+00:00").status_code == 422
    assert send(client, stamp="2026-01-01T12:00:00").status_code == 422
    assert (
        send(
            client, stamp=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/cameras/casa/images", headers={"Authorization": "Bearer " + TOKEN}
        ).status_code
        == 415
    )
    monkeypatch.setenv("MAX_IMAGE_BYTES", "10")
    with TestClient(create_app(tmp_path / "small", {"casa": TOKEN})) as small:
        assert send(small).status_code == 413


def test_upload_download_and_isolation(client):
    assert client.get("/api/v1/cameras/casa/latest").status_code == 404
    assert client.get("/api/v1/cameras/casa/status").json() == {"latest": None}
    result = send(client)
    assert result.status_code == 201
    url = result.json()["url"]
    response = client.get(url + "?download=true")
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"].startswith("attachment;")
    Image.open(io.BytesIO(response.content)).verify()
    assert client.get(url.replace("/casa/", "/altra/")).status_code == 404
    assert (
        client.get("/api/v1/cameras/casa/history").json()["images"][0]["id"]
        == result.json()["id"]
    )
    assert TOKEN not in client.get("/api/v1/cameras").text
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_hourly_retention_latest_and_restart(client):
    now = datetime.now(timezone.utc).replace(
        minute=30, second=0, microsecond=0
    ) - timedelta(hours=1)
    first = send(client, now.isoformat()).json()
    second = send(client, (now + timedelta(minutes=1)).isoformat()).json()
    third = send(client, (now + timedelta(minutes=2)).isoformat()).json()
    assert (
        client.get("/api/v1/cameras/casa/history").json()["images"][0]["id"]
        == first["id"]
    )
    assert (
        client.get("/api/v1/cameras/casa/status").json()["latest"]["id"] == third["id"]
    )
    assert client.get(second["url"]).status_code == 404
    with sqlite3.connect(client.db) as db:
        assert db.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 2
    with TestClient(create_app(client.db.parent, {"casa": TOKEN})) as restarted:
        assert restarted.get(third["url"]).status_code == 200


def test_retention_and_out_of_order(client):
    now = datetime.now(timezone.utc)
    for age in reversed(range(24)):
        assert send(client, (now - timedelta(hours=age)).isoformat()).status_code == 201
    newest = client.get("/api/v1/cameras/casa/status").json()["latest"]["id"]
    send(client, (now - timedelta(hours=2, minutes=1)).isoformat())
    assert client.get("/api/v1/cameras/casa/status").json()["latest"]["id"] == newest
    with sqlite3.connect(client.db) as db:
        db.execute(
            "UPDATE images SET captured=? WHERE id=(SELECT id FROM images ORDER BY captured LIMIT 1)",
            ((now - timedelta(hours=25)).timestamp(),),
        )
    assert len(client.get("/api/v1/cameras/casa/history").json()["images"]) == 23
    send(client)
    with sqlite3.connect(client.db) as db:
        assert db.execute("SELECT COUNT(*) FROM images").fetchone()[0] <= 25
        assert (
            db.execute("SELECT MIN(captured) FROM images").fetchone()[0]
            > (now - timedelta(hours=24)).timestamp()
        )


def test_concurrent_uploads_keep_single_hourly(client):
    stamp = (
        datetime.now(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: send(client, stamp), range(8)))
    assert all(result.status_code == 201 for result in results)
    assert len(client.get("/api/v1/cameras/casa/history").json()["images"]) == 1
    with sqlite3.connect(client.db) as db:
        assert db.execute("SELECT COUNT(*) FROM images").fetchone()[0] <= 2


def test_configuration_requires_secret(tmp_path):
    with pytest.raises(ValueError):
        create_app(tmp_path, {})
    with pytest.raises(ValueError):
        create_app(tmp_path, {"../bad": "short"})
