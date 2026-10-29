from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from flow_api import app
from flow_handoff import flow_logged_in
from flow_web import FlowWebClient


def test_flow_logged_in_no_file():
    with tempfile.TemporaryDirectory() as td:
        assert flow_logged_in(Path(td)) is False


def test_flow_client_status():
    client = FlowWebClient()
    st = client.status()
    assert "logged_in" in st
    assert "profile_dir" in st
    assert "5d272dab-59e3-4a29-9297-b139770754b8" in st["project_url"]


def test_flow_api_status():
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "logged_in" in data
    assert "project_url" in data


def test_flow_api_video_missing_file():
    client = TestClient(app)
    response = client.post(
        "/api/video",
        json={"image_path": "/nonexistent/test_img.png", "prompt": "loop video"},
    )
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"].lower()


@patch("flow_api.client.is_logged_in", return_value=False)
def test_flow_api_video_not_logged_in(mock_logged_in):
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        client = TestClient(app)
        response = client.post(
            "/api/video",
            json={"image_path": tf.name, "prompt": "loop video"},
        )
        assert response.status_code == 401
        assert "not logged in" in response.json()["detail"].lower()


@patch("flow_api.client.is_logged_in", return_value=True)
@patch(
    "flow_api.client.generate_video",
    new_callable=AsyncMock,
    return_value={
        "success": True,
        "prompt": "create a seamless loop video of 10s",
        "image_input": "/path/to/test.png",
        "video_path": "/path/to/video.mp4",
        "file_size": 1048576,
        "duration": 10,
        "project_url": "https://flow.google.com/project/5d272dab-59e3-4a29-9297-b139770754b8",
    },
)
def test_flow_api_video_success(mock_gen, mock_logged_in):
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        client = TestClient(app)
        response = client.post(
            "/api/video",
            json={
                "image_path": tf.name,
                "prompt": "create a seamless loop video of 10s",
                "duration": 10,
                "loop": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["duration"] == 10
        assert data["video_path"] == "/path/to/video.mp4"


def test_flow_api_rejects_unsupported_duration():
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        response = TestClient(app).post("/api/video", json={"image_path": tf.name, "duration": 5})
        assert response.status_code == 422


def test_flow_api_cors_only_allows_local_pages():
    client = TestClient(app)
    headers = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Content-Type"}
    local = client.options("/api/video", headers={**headers, "Origin": "http://localhost:3000"})
    external = client.options("/api/video", headers={**headers, "Origin": "https://example.com"})
    assert local.status_code == 200
    assert external.status_code == 400


@pytest.mark.asyncio
async def test_flow_client_rejects_external_project_url():
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        client = FlowWebClient()
        with pytest.raises(ValueError, match="Google Flow project URL"):
            await client.generate_video(tf.name, project_url="https://example.com/project/5d272dab-59e3-4a29-9297-b139770754b8")


@pytest.mark.asyncio
async def test_flow_client_stop_when_locked():
    client = FlowWebClient()
    async with client.lock:
        await client.stop()
    assert client.process is None
