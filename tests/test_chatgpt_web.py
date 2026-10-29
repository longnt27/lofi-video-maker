from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from chatgpt_api import app
from chatgpt_handoff import chatgpt_logged_in, clean_reply_text
from chatgpt_web import ChatGPTWebClient


def test_clean_reply_text():
    assert clean_reply_text("Here is the revised text:\nHello world") == "Hello world"
    assert clean_reply_text("Certainly! Here is your answer:\n42") == "42"
    assert clean_reply_text("```json\n{\"ok\": true}\n```") == '{"ok": true}'
    assert clean_reply_text("   Simple text   ") == "Simple text"


def test_chatgpt_logged_in_no_file():
    with tempfile.TemporaryDirectory() as td:
        assert chatgpt_logged_in(Path(td)) is False


def test_client_status():
    client = ChatGPTWebClient()
    st = client.status()
    assert "logged_in" in st
    assert "profile_dir" in st
    assert st["image_chat_name"] == "[Image gen]"


def test_api_status_endpoint():
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "logged_in" in data
    assert "profile_dir" in data
    assert data["image_chat_name"] == "[Image gen]"


@patch("chatgpt_api.client.is_logged_in", return_value=False)
def test_api_image_not_logged_in(mock_logged_in):
    client = TestClient(app)
    response = client.post("/api/image", json={"prompt": "A cat"})
    assert response.status_code == 401
    assert "not logged in" in response.json()["detail"].lower()


@patch("chatgpt_api.client.is_logged_in", return_value=True)
@patch(
    "chatgpt_api.client.generate_image",
    new_callable=AsyncMock,
    return_value={
        "success": True,
        "prompt": "A sunset over mountains",
        "chat_name": "[Image gen]",
        "chat_url": "https://chatgpt.com/c/6ae2a7c3-8c40-83ec-954b-17b9b8a21f29",
        "text": "Here is your generated image.",
        "images": [
            {
                "index": 1,
                "file_path": "/path/to/img1.webp",
                "width": 1672,
                "height": 941,
                "alt": "Generated image 1",
                "base64": "data:image/webp;base64,mockdata",
            }
        ],
    },
)
def test_api_image_generation_success(mock_gen, mock_logged_in):
    client = TestClient(app)
    response = client.post(
        "/api/image",
        json={"prompt": "A sunset over mountains", "include_base64": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["images"]) == 1
    assert data["images"][0]["file_path"] == "/path/to/img1.webp"
    assert data["images"][0]["base64"] == "data:image/webp;base64,mockdata"


@patch("chatgpt_api.client.is_logged_in", return_value=True)
@patch("chatgpt_api.client.ask_text", new_callable=AsyncMock, return_value="The answer is 42")
def test_api_chat_text_success(mock_ask, mock_logged_in):
    client = TestClient(app)
    response = client.post(
        "/api/chat",
        json={"prompt": "What is 40 + 2?", "temporary": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["reply"] == "The answer is 42"


@pytest.mark.asyncio
async def test_client_stop_when_locked():
    client = ChatGPTWebClient()
    async with client.lock:
        await client.stop()
    assert client.process is None
