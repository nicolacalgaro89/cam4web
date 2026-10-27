from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import pytest

from agent import cam4web_agent as agent

URL = "https://example.com"


def test_offline_queue_reconnect_and_restart(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    stamps = [(now - timedelta(hours=age)).isoformat() for age in (3, 2, 1)]
    queue = agent.HourlyQueue(tmp_path, URL, "casa")

    def offline(*args):
        raise URLError("offline")

    monkeypatch.setattr(agent, "upload", offline)
    for stamp in stamps:
        with pytest.raises(URLError):
            agent.deliver(queue, URL, "casa", "secret", b"jpeg", stamp)
    queue = agent.HourlyQueue(tmp_path, URL, "casa")
    assert [row[0] for row in queue.pending(now.timestamp())] == stamps
    sent = []
    monkeypatch.setattr(agent, "upload", lambda *args: sent.append(args[-1]))
    current = now.isoformat()
    agent.deliver(queue, URL, "casa", "secret", b"current", current)
    assert sent == [current, *stamps]
    assert queue.pending(now.timestamp()) == []
    # A restart and another capture in the same hour do not recreate a delivered sample.
    queue = agent.HourlyQueue(tmp_path, URL, "casa")
    queue.retain(b"later", (now + timedelta(seconds=1)).isoformat())
    assert queue.pending(now.timestamp()) == []


def test_first_sample_expiry_limit_and_scope(tmp_path):
    now = datetime.now(timezone.utc)
    queue = agent.HourlyQueue(tmp_path, URL, "casa")
    for age in reversed(range(27)):
        stamp = (now - timedelta(hours=age)).isoformat()
        queue.retain(b"first", stamp)
        queue.retain(b"second", stamp)
    with queue.connect() as db:
        rows = db.execute("SELECT stamp,jpeg FROM samples").fetchall()
    assert len(rows) == 24
    assert all(jpeg == b"first" for _, jpeg in rows)
    assert queue.pending((now + timedelta(hours=24)).timestamp()) == []
    queue.retain(b"private", now.isoformat())
    assert agent.HourlyQueue(tmp_path, URL, "altra").pending(now.timestamp()) == []
    assert (
        agent.HourlyQueue(tmp_path, "https://other.example", "casa").pending(
            now.timestamp()
        )
        == []
    )


def test_failed_backlog_is_kept_and_success_is_not_repeated(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    queue = agent.HourlyQueue(tmp_path, URL, "casa")
    old = (now - timedelta(hours=2)).isoformat()
    newer = (now - timedelta(hours=1)).isoformat()
    queue.retain(b"old", old)
    queue.retain(b"newer", newer)
    sent = []

    def upload(*args):
        if args[-1] == newer:
            raise URLError("connection lost")
        sent.append(args[-1])

    monkeypatch.setattr(agent, "upload", upload)
    with pytest.raises(URLError):
        agent.deliver(queue, URL, "casa", "secret", b"current", now.isoformat())
    assert sent == [now.isoformat(), old]
    assert [row[0] for row in queue.pending(now.timestamp())] == [newer]


def test_recovered_images_populate_server_history(tmp_path, monkeypatch):
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from server.main import create_app

    token = "test-token-with-at-least-24-characters"
    now = datetime.now(timezone.utc)
    queue = agent.HourlyQueue(tmp_path / "queue", URL, "casa")
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(output, "JPEG")
    jpeg = output.getvalue()
    for age in (3, 2, 1):
        queue.retain(jpeg, (now - timedelta(hours=age)).isoformat())

    with TestClient(create_app(tmp_path / "server", {"casa": token})) as client:

        def upload(url, camera, secret, body, timestamp):
            response = client.post(
                f"/api/v1/cameras/{camera}/images",
                content=body,
                headers={
                    "Authorization": "Bearer " + secret,
                    "Content-Type": "image/jpeg",
                    "X-Captured-At": timestamp,
                },
            )
            response.raise_for_status()

        monkeypatch.setattr(agent, "upload", upload)
        agent.deliver(queue, URL, "casa", token, jpeg, now.isoformat())
        history = client.get("/api/v1/cameras/casa/history").json()["images"]
        assert len(history) == 4
        latest = client.get("/api/v1/cameras/casa/status").json()["latest"]
        assert datetime.fromisoformat(latest["captured_at"]) == now
        assert queue.pending(now.timestamp()) == []
