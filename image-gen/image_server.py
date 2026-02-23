"""
Local Image Generation Service
-------------------------------
Lightweight FastAPI server using Stable Diffusion (SDXL-Turbo) via HuggingFace
diffusers for fast comic panel generation.

Endpoints:
  POST /generate   → Generate a single image from a text prompt
  GET  /health     → Health check
  GET  /images/{filename} → Serve generated images
"""

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import torch
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

MODEL_ID = os.environ.get("SD_MODEL", "stabilityai/sdxl-turbo")
OUTPUT_DIR = Path(os.environ.get("IMAGE_OUTPUT_DIR", "/images"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32

# ──────────────────────────────────────────────────────────────────────────────
# Model loading (lazy singleton)
# ──────────────────────────────────────────────────────────────────────────────

_pipeline = None
_model_lock = asyncio.Lock()


def _load_pipeline():
    """Load the SD pipeline once. Called at startup or on first request."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    print(f"[image-gen] Loading model {MODEL_ID} on {DEVICE} ({DTYPE})...")
    start = time.time()

    # Import here to avoid top-level crash if versions mismatch
    from diffusers import StableDiffusionXLPipeline

    if MODEL_ID == "stabilityai/sdxl-turbo":
        from diffusers import AutoPipelineForText2Image
        _pipeline = AutoPipelineForText2Image.from_pretrained(
            MODEL_ID,
            torch_dtype=DTYPE,
            variant="fp16" if DTYPE == torch.float16 else None,
        )
    else:
        _pipeline = StableDiffusionXLPipeline.from_pretrained(
            MODEL_ID,
            torch_dtype=DTYPE,
            variant="fp16" if DTYPE == torch.float16 else None,
        )

    _pipeline = _pipeline.to(DEVICE)

    # Optimizations
    if DEVICE == "cuda":
        try:
            _pipeline.enable_xformers_memory_efficient_attention()
        except Exception:
            pass  # xformers not available, that's fine

    elapsed = time.time() - start
    print(f"[image-gen] Model loaded in {elapsed:.1f}s")
    return _pipeline


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Image Generation Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    prompt: str
    negative_prompt: Optional[str] = "blurry, low quality, distorted, deformed, text, watermark"
    width: int = 512
    height: int = 512
    num_inference_steps: int = 4  # SDXL-Turbo needs only 1-4 steps
    guidance_scale: float = 0.0  # SDXL-Turbo works best with 0.0
    seed: Optional[int] = None
    style: str = "comic"  # comic | realistic | fantasy


# Style prompt modifiers for different art styles
STYLE_PREFIXES = {
    "comic": "comic book art style, bold outlines, vibrant colors, dynamic composition, detailed illustration, graphic novel panel, ",
    "manga": "manga art style, black and white, dramatic shading, expressive characters, Japanese comic style, ",
    "fantasy": "fantasy art, highly detailed, ethereal lighting, epic scene, digital painting, concept art, ",
    "realistic": "photorealistic, highly detailed, cinematic lighting, 8k, masterpiece, ",
    "watercolor": "watercolor painting style, soft colors, flowing brushstrokes, artistic, ",
}

STYLE_NEGATIVES = {
    "comic": "photorealistic, photograph, 3d render",
    "manga": "color, photorealistic, 3d render",
    "fantasy": "cartoon, low quality, simple",
    "realistic": "cartoon, anime, illustration",
    "watercolor": "photorealistic, sharp edges, digital",
}


@app.on_event("startup")
async def startup():
    """Pre-load the model on startup."""
    await asyncio.get_event_loop().run_in_executor(None, _load_pipeline)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model": MODEL_ID,
        "device": DEVICE,
        "model_loaded": _pipeline is not None,
    }


@app.post("/generate")
async def generate_image(req: GenerateRequest):
    """Generate a single image from a text prompt."""
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")

    async with _model_lock:
        pipe = _pipeline
        if pipe is None:
            pipe = await asyncio.get_event_loop().run_in_executor(None, _load_pipeline)

    # Build styled prompt
    style_prefix = STYLE_PREFIXES.get(req.style, STYLE_PREFIXES["comic"])
    style_negative = STYLE_NEGATIVES.get(req.style, "")
    full_prompt = style_prefix + req.prompt
    full_negative = (req.negative_prompt or "") + ", " + style_negative

    # Generate
    generator = None
    if req.seed is not None:
        generator = torch.Generator(device=DEVICE).manual_seed(req.seed)

    start = time.time()

    def _generate():
        with torch.no_grad():
            result = pipe(
                prompt=full_prompt,
                negative_prompt=full_negative,
                width=req.width,
                height=req.height,
                num_inference_steps=req.num_inference_steps,
                guidance_scale=req.guidance_scale,
                generator=generator,
            )
        return result.images[0]

    image = await asyncio.get_event_loop().run_in_executor(None, _generate)
    elapsed = time.time() - start

    # Save image
    filename = f"{uuid.uuid4().hex[:12]}.png"
    filepath = OUTPUT_DIR / filename
    image.save(str(filepath))

    return {
        "filename": filename,
        "url": f"/images/{filename}",
        "width": req.width,
        "height": req.height,
        "elapsed_seconds": round(elapsed, 2),
        "prompt": req.prompt,
        "style": req.style,
    }


@app.get("/images/{filename}")
async def serve_image(filename: str):
    """Serve a generated image."""
    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(filepath), media_type="image/png")


# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
