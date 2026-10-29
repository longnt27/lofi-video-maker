from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from .flow_web import FlowWebClient
    from .flow_handoff import DEFAULT_FLOW_PROJECT_URL
except (ImportError, ValueError):
    from flow_web import FlowWebClient
    from flow_handoff import DEFAULT_FLOW_PROJECT_URL

app = FastAPI(
    title="Google Flow Web API",
    description="Browser-automated Google Flow API for image-to-video looping generation",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

client = FlowWebClient()


class VideoGenerationRequest(BaseModel):
    image_path: str = Field(..., description="Local file path of the input reference image")
    prompt: str = Field("Animate this image", description="Prompt instructing video motion")
    duration: Literal[4, 6, 8, 10] = Field(10, description="Video duration in seconds")
    loop: bool = Field(True, description="Whether to instruct model for seamless looping")
    project_url: Optional[str] = Field(DEFAULT_FLOW_PROJECT_URL, description="Google Flow Project URL")
    output_path: Optional[str] = Field(None, description="Local path to save output video (MP4)")
    timeout: float = Field(600.0, ge=30.0, le=1800.0, description="Timeout in seconds for video generation")


class VideoGenerationResponse(BaseModel):
    success: bool
    prompt: str
    image_input: str
    video_path: str
    file_size: int
    duration: int
    project_url: str


class FlowStatusResponse(BaseModel):
    logged_in: bool
    profile_dir: str
    profile_exists: bool
    project_url: str


@app.get("/api/status", response_model=FlowStatusResponse)
async def get_status():
    return client.status()


@app.post("/api/login")
async def open_login():
    res = await client.open_login()
    return res


@app.post("/api/video", response_model=VideoGenerationResponse)
async def generate_video(req: VideoGenerationRequest):
    img_p = Path(req.image_path)
    if not img_p.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Image file does not exist: {req.image_path}",
        )

    if not client.is_logged_in():
        raise HTTPException(
            status_code=401,
            detail="Google Flow is not logged in. Use POST /api/login or run `python -m flow_web --login`.",
        )

    try:
        res = await client.generate_video(
            image_path=req.image_path,
            prompt=req.prompt,
            duration=req.duration,
            loop=req.loop,
            project_url=req.project_url,
            output_path=req.output_path,
            timeout=req.timeout,
        )
        return res
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Google Flow Web API Server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on")
    args = parser.parse_args()

    print(f"Starting Google Flow Web API server on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
