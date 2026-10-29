"""Local Gradio UI adapted from the user's LaMa/Fallin Colab notebook.

Run: python lofi_studio.py --device cuda
Fallin by renarchi (CC BY 4.0): https://github.com/renarchi/Re-SISR
"""
from __future__ import annotations

import argparse
import gc
import time
import uuid
from pathlib import Path


def build_app(home: Path, device: str = "auto"):
    import cv2
    import gradio as gr
    import numpy as np
    from gradio_image_annotation import image_annotator
    from fallin_preprocess import FrameProcessor, media_info, output_size, stream_video

    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    active = [None, None]

    def get_processor(variant, tile):
        key = (variant, bool(tile))
        if active[0] != key:
            # Keep only the chosen Fallin variant, rather than all GPU sessions.
            active[1] = None
            gc.collect()
            active[1] = FrameProcessor(home/"models", variant, device, 512 if tile else 0)
            active[0] = key
        return active[1]

    def first_frame(path):
        if not path:
            raise gr.Error("Upload video trước.")
        info = media_info(Path(path))
        cap = cv2.VideoCapture(str(path))
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok:
            raise gr.Error("Không đọc được frame đầu.")
        return {"image": cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), "boxes": []}, (
            f'{info["width"]}×{info["height"]} | {float(info["fps"]):.3f} FPS. '
            "Khoanh logo nếu bật xóa watermark.")

    def selection(ann, enabled, info):
        if not enabled:
            return None
        boxes = ann.get("boxes", []) if ann else []
        if len(boxes) != 1:
            raise gr.Error("Hãy khoanh đúng một vùng watermark.")
        b = boxes[0]
        x, y = int(b["xmin"]), int(b["ymin"])
        w, h = int(b["xmax"])-x, int(b["ymax"])-y
        if min(w, h) <= 0 or min(x, y) < 0 or x+w > info["width"] or y+h > info["height"]:
            raise gr.Error("Vùng watermark không hợp lệ.")
        if w*h > info["width"]*info["height"]*0.25:
            raise gr.Error("Vùng watermark quá lớn (>25% khung hình).")
        return x, y, w, h

    def edge_value(edge):
        return None if edge == "Native AI 2x" else int(edge)

    def preview(path, ann, remove, variant, edge, grain, tile):
        if not path:
            raise gr.Error("Upload video trước.")
        info = media_info(Path(path))
        size = output_size(info["width"], info["height"], edge_value(edge))
        box = selection(ann, remove, info)
        proc = get_processor(variant, tile)
        cap = cv2.VideoCapture(str(path))
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok:
            raise gr.Error("Không đọc được video.")
        start = time.perf_counter()
        clean = proc.lama_frame(frame, proc.crop_settings(box, info["width"], info["height"]), grain) if box else frame
        up = proc.upscale(clean, size)
        basic = cv2.resize(frame, size, interpolation=cv2.INTER_LANCZOS4)
        sz = min(480, *size)
        # Compare a central texture crop, independently of the watermark repair.
        x, y = (size[0]-sz)//2, (size[1]-sz)//2
        comp = np.concatenate([basic[y:y+sz, x:x+sz], up[y:y+sz, x:x+sz]], axis=1)
        return cv2.cvtColor(comp, cv2.COLOR_BGR2RGB), cv2.cvtColor(up, cv2.COLOR_BGR2RGB), (
            f"{size[0]}×{size[1]} | {time.perf_counter()-start:.1f}s/frame (lần đầu gồm tải model). "
            "Crop trái: Lanczos; phải: Fallin. Kiểm tra thêm video để phát hiện nhấp nháy.")

    def export(path, ann, remove, variant, edge, seconds, grain, tile, progress=gr.Progress()):
        if not path:
            raise gr.Error("Upload video trước.")
        info = media_info(Path(path))
        box = selection(ann, remove, info)
        proc = get_processor(variant, tile)
        dest = home/"outputs"/f"clean_{variant}_{uuid.uuid4().hex[:10]}.mp4"
        start = time.perf_counter()
        result = stream_video(Path(path), dest, proc, box=box, short_edge=edge_value(edge),
                              grain=grain if box else 0, seconds=seconds, keep_audio=True,
                              progress=lambda done, total: progress(min(done/total, 0.99), desc=f"{done}/{total} frame"))
        progress(1, desc="Hoàn tất")
        return str(dest), str(dest), (
            f'{result["width"]}×{result["height"]}, {float(result["fps"]):.3f} FPS, '
            f'{result["frames"]} frame | {time.perf_counter()-start:.1f}s. '
            "Giữ âm thanh nếu nguồn có; Fallin AI 2×, có thể thu nhỏ theo lựa chọn.")

    with gr.Blocks(title="LoFi Studio · LaMa + Fallin", theme=gr.themes.Soft()) as app:
        gr.Markdown("## LoFi Studio · LaMa → Fallin → MP4\nUpload một lần, giữ FPS nguồn. Fallin: renarchi, CC BY 4.0.")
        with gr.Row():
            with gr.Column():
                source = gr.File(label="Video gốc", file_types=["video"], type="filepath")
                pick = gr.Button("Lấy frame đầu để khoanh watermark")
                ann = image_annotator(label="Vùng watermark", image_type="numpy", single_box=True)
                remove = gr.Checkbox(value=True, label="Xóa watermark bằng LaMa")
                variant = gr.Radio(["soft", "strong"], value="soft", label="Fallin (Strong có thể quá sắc)")
                edge = gr.Dropdown(["Native AI 2x", "1080", "1440", "2160"], value="Native AI 2x", label="Cạnh ngắn đầu ra; không vượt AI 2×")
                seconds = gr.Slider(1, 60, value=10, step=1, label="Số giây xử lý")
                grain = gr.Slider(0, 5, value=0, step=0.1, label="Grain vùng sửa")
                tile = gr.Checkbox(value=True, label="Chia ô 512px để giảm VRAM")
                preview_btn = gr.Button("Xem thử một frame")
                export_btn = gr.Button("Xóa watermark + upscale → MP4", variant="primary")
            with gr.Column():
                report = gr.Textbox(label="Trạng thái")
                comparison = gr.Image(label="Crop: Lanczos / Fallin")
                full = gr.Image(label="Frame đầy đủ (giao diện có thể thu nhỏ)")
                video = gr.Video(label="MP4 kết quả")
                download = gr.File(label="Tải MP4")
        pick.click(first_frame, inputs=source, outputs=[ann, report], concurrency_id="studio", concurrency_limit=1)
        source.change(lambda: (None, "Hãy lấy frame và khoanh lại vùng watermark cho video mới."),
                      outputs=[ann, report], concurrency_id="studio", concurrency_limit=1)
        common = [source, ann, remove, variant, edge]
        preview_btn.click(preview, inputs=common+[grain, tile], outputs=[comparison, full, report], concurrency_id="studio", concurrency_limit=1)
        export_btn.click(export, inputs=common+[seconds, grain, tile], outputs=[video, download, report], concurrency_id="studio", concurrency_limit=1)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("cache/studio"))
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="Explicitly enable a public Gradio share link")
    args = parser.parse_args()
    build_app(args.cache_dir, args.device).queue(default_concurrency_limit=1).launch(
        server_name=args.host, server_port=args.port, share=args.share, show_error=True)


if __name__ == "__main__":
    main()
