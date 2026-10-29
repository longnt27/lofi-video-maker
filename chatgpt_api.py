from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from .chatgpt_web import ChatGPTWebClient
except (ImportError, ValueError):
    from chatgpt_web import ChatGPTWebClient

app = FastAPI(
    title="ChatGPT Web API",
    description="Browser-automated ChatGPT Web API for image and text generation",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = ChatGPTWebClient()


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., description="Prompt describing the image to generate")
    chat_name: Optional[str] = Field("[Image gen]", description="Name of the ChatGPT chat to use")
    chat_url: Optional[str] = Field(None, description="Direct URL to chat, if known")
    output_path: Optional[str] = Field(None, description="Local file path to save the generated image")
    include_base64: bool = Field(False, description="Whether to include full base64 image data in response")
    timeout: float = Field(180.0, ge=10.0, le=600.0, description="Generation timeout in seconds")


class ImageItem(BaseModel):
    index: int
    file_path: Optional[str]
    width: Optional[int]
    height: Optional[int]
    alt: Optional[str]
    base64: Optional[str] = None


class ImageGenerationResponse(BaseModel):
    success: bool
    prompt: str
    chat_name: Optional[str]
    chat_url: Optional[str]
    text: Optional[str]
    images: list[ImageItem]


class TextPromptRequest(BaseModel):
    prompt: str = Field(..., description="Prompt for text generation")
    temporary: bool = Field(True, description="Whether to use a temporary chat")
    timeout: float = Field(180.0, ge=10.0, le=600.0, description="Timeout in seconds")


class TextPromptResponse(BaseModel):
    success: bool
    prompt: str
    reply: str


class StatusResponse(BaseModel):
    logged_in: bool
    profile_dir: str
    profile_exists: bool
    image_chat_name: str
    image_chat_url: str


@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    return client.status()


@app.post("/api/login")
async def open_login():
    res = await client.open_login()
    return res


@app.post("/api/image", response_model=ImageGenerationResponse)
async def generate_image(req: ImageGenerationRequest):
    if not client.is_logged_in():
        raise HTTPException(
            status_code=401,
            detail="ChatGPT is not logged in. Use POST /api/login or check the browser profile.",
        )
    try:
        res = await client.generate_image(
            prompt=req.prompt,
            chat_name=req.chat_name,
            chat_url=req.chat_url,
            output_path=req.output_path,
            timeout=req.timeout,
        )
        if not req.include_base64:
            for img in res.get("images", []):
                img["base64"] = None
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat", response_model=TextPromptResponse)
async def send_text_prompt(req: TextPromptRequest):
    if not client.is_logged_in():
        raise HTTPException(
            status_code=401,
            detail="ChatGPT is not logged in. Use POST /api/login or check the browser profile.",
        )
    try:
        reply = await client.ask_text(
            prompt=req.prompt,
            temporary=req.temporary,
            timeout=req.timeout,
        )
        return {"success": True, "prompt": req.prompt, "reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def run_server(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn
    uvicorn.run("lofi_maker.chatgpt_api:app" if Path("chatgpt_api.py").exists() else app, host=host, port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChatGPT Web API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
