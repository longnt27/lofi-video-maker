"""Local Gradio UI adapted from the user's LaMa/Fallin Colab notebook.

Run: python lofi_studio.py --device cuda
Fallin by renarchi (CC BY 4.0): https://github.com/renarchi/Re-SISR
"""
from __future__ import annotations

import argparse
import gc
from collections import deque
import time
from pathlib import Path


def build_app(home: Path, device: str = "auto"):
    import cv2
    import gradio as gr
    import numpy as np
    from gradio_image_annotation import image_annotator
    from fallin_preprocess import FrameProcessor, media_info, output_size
    from studio_pipeline import JobRunner, choose_folder, collect_music, clock, read_state, open_output_folder

    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    active = [None, None]
    runner = JobRunner(home)

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

    def base_config(path, ann, remove, variant, edge, seconds, grain, tile, fps):
        if not path:
            raise gr.Error("Upload video trước.")
        info = media_info(Path(path))
        box = selection(ann, remove, info)
        output_size(info["width"], info["height"], edge_value(edge))
        return dict(source=str(Path(path).resolve()), variant=variant, device=device,
                    box=list(box) if box else None, edge=edge_value(edge), grain=grain if box else 0,
                    seconds=seconds, tile=bool(tile), fps=None if fps == "source" else int(fps))

    def execute(config, progress):
        # Release preview GPU models before launching the worker's ONNX sessions.
        active[:] = [None, None]
        gc.collect()
        try:
            job = runner.start(config)
        except OSError as exc:
            raise gr.Error(f"Không ghi được vào thư mục lưu. Chọn thư mục khác: {exc}") from exc
        start = time.perf_counter()
        finalized = False
        try:
            while job.process.poll() is None:
                state = read_state(job.folder)
                fraction = state.get("progress")
                stage = state.get("stage", "Đang xử lý")
                progress((min(fraction, 0.99) if fraction is not None else None), desc=stage)
                suffix = f" ({fraction*100:.0f}%)" if fraction is not None else ""
                yield gr.skip(), f"{stage}{suffix} | {time.perf_counter()-start:.0f}s"
                time.sleep(0.4)
            state = runner.finish(job)
            finalized = True
            if state["status"] == "done":
                progress(1, desc="Hoàn tất")
                # Text only: no Gradio File/Video postprocessing, hashing, copying
                # or browser download of potentially multi-GB output.
                yield state["output"], state["stage"]+"\nĐã lưu trực tiếp trên máy, không cần tải về."
            elif state["status"] == "cancelled":
                yield gr.skip(), state["stage"]
            else:
                log = job.folder/"worker.log"
                tail = ""
                if log.exists():
                    with log.open(encoding="utf-8", errors="replace") as stream:
                        tail = "".join(deque(stream, maxlen=10))[-1800:]
                yield gr.skip(), f'{state["stage"]}\nLog: {log}\n{tail}'
        finally:
            if not finalized:
                runner.cancel()
                job.process.wait(timeout=5)
                runner.finish(job)

    def export_clip(path, ann, remove, variant, edge, seconds, grain, tile, fps, output_dir, progress=gr.Progress()):
        config = base_config(path, ann, remove, variant, edge, seconds, grain, tile, fps)
        yield from execute({**config, "kind": "clip", "output_dir": output_dir}, progress)

    def export_mix(path, ann, remove, variant, edge, seconds, grain, tile, fps,
                   music, recursive, shuffle, seed, crossfade, fade, seamless, overlap, dust,
                   duration_choice, output_dir, progress=gr.Progress()):
        config = base_config(path, ann, remove, variant, edge, seconds, grain, tile, fps)
        try:
            collect_music(music, recursive, shuffle, int(seed), crossfade)
        except ValueError as exc:
            raise gr.Error(str(exc)) from exc
        if fps == "source" and media_info(Path(path))["fps"] not in (24, 25, 30, 50, 60):
            raise gr.Error("Xuất video dài cần FPS nguyên: chọn 24/25/30/50/60.")
        if seamless and overlap*2 >= min(seconds, media_info(Path(path))["duration"]):
            raise gr.Error("Thời lượng hòa điểm nối phải ngắn hơn nửa clip nền. Giảm overlap rồi thử lại.")
        preview_seconds = {"30 giây": 30, "125 giây": 125, "Toàn bộ playlist": None}[duration_choice]
        config.update(kind="mix", music=music, recursive=recursive, shuffle=shuffle, seed=int(seed),
            crossfade=crossfade, fade=fade, seamless=seamless, overlap=overlap,
            dust=dust, preview=preview_seconds, codec="h264", output_dir=output_dir)
        yield from execute(config, progress)

    def pick_music():
        try:
            selected = choose_folder()
            return selected if selected else gr.skip()
        except Exception as exc:
            raise gr.Error("Không mở được cửa sổ chọn thư mục. Dán đường dẫn folder vào ô bên dưới.") from exc

    def pick_output():
        try:
            selected = choose_folder("Chọn thư mục lưu video")
            return selected if selected else gr.skip()
        except Exception as exc:
            raise gr.Error("Không mở được cửa sổ chọn thư mục. Dán đường dẫn thư mục lưu vào ô bên dưới.") from exc

    def open_result(saved_path, folder):
        return open_output_folder(saved_path, folder, home)

    def inspect_music(folder, recursive, shuffle, seed, crossfade):
        if not folder or not folder.strip():
            return [], "Chọn thư mục nhạc để xem playlist."
        try:
            playlist = collect_music(folder, recursive, shuffle, int(seed), crossfade)
            rows = [[i+1, name, clock(seconds)] for i, (name, seconds) in enumerate(zip(playlist["names"], playlist["durations"]))]
            return rows, (f'{len(rows)} bài | tổng nhạc {clock(sum(playlist["durations"]))} | '
                          f'video đầy đủ khoảng {clock(playlist["duration"])} sau crossfade.')
        except (ValueError, OSError) as exc:
            return [], str(exc)

    with gr.Blocks(title="LoFi Studio · LaMa + Fallin") as app:
        gr.Markdown("## LoFi Studio · Xử lý clip → Loop → Ghép nhạc → MP4\nChọn clip và thư mục nhạc trong cùng giao diện. Fallin: renarchi, CC BY 4.0.")
        with gr.Row():
            with gr.Column():
                source = gr.File(label="Video gốc", file_types=["video"], type="filepath")
                pick = gr.Button("Lấy frame đầu để khoanh watermark")
                ann = image_annotator(label="Vùng watermark", image_type="numpy", single_box=True)
                remove = gr.Checkbox(value=True, label="Xóa watermark bằng LaMa")
                variant = gr.Radio(["soft", "strong"], value="soft", label="Fallin (Strong có thể quá sắc)")
                edge = gr.Dropdown(["Native AI 2x", "1080", "1440", "2160"], value="Native AI 2x", label="Cạnh ngắn đầu ra; không vượt AI 2×")
                seconds = gr.Slider(1, 60, value=10, step=1, label="Số giây đầu của clip dùng làm nền (cả hai kiểu xuất)")
                fps = gr.Dropdown(["source", "24", "25", "30", "50", "60"], value="source", label="FPS: source = giữ nguồn; video dài cần FPS nguyên")
                grain = gr.Slider(0, 5, value=0, step=0.1, label="Grain vùng sửa")
                tile = gr.Checkbox(value=True, label="Chia ô 512px để giảm VRAM")
                preview_btn = gr.Button("Xem thử một frame")
                output_dir = gr.Textbox(value=str(home/"outputs"), label="Thư mục lưu video trên máy", placeholder=r"D:\LoFi Exports")
                output_folder_btn = gr.Button("Chọn thư mục lưu video")
                export_btn = gr.Button("Xuất clip đã xử lý")
                with gr.Accordion("Nhạc và video dài", open=True):
                    folder_btn = gr.Button("Chọn thư mục nhạc trên máy")
                    music = gr.Textbox(label="Đường dẫn thư mục nhạc", placeholder=r"D:\Music\LoFi")
                    recursive = gr.Checkbox(value=False, label="Lấy cả nhạc trong thư mục con")
                    shuffle = gr.Checkbox(value=False, label="Xáo trộn thứ tự bài")
                    seed = gr.Number(value=42, precision=0, label="Seed xáo trộn")
                    crossfade = gr.Slider(0, 10, value=2, step=0.1, label="Crossfade giữa hai bài (giây)")
                    scan_btn = gr.Button("Đọc lại danh sách nhạc")
                    playlist_table = gr.Dataframe(headers=["STT", "Bài nhạc", "Thời lượng"], datatype=["number", "str", "str"], interactive=False)
                    playlist_summary = gr.Textbox(label="Thời lượng playlist", interactive=False)
                    fade = gr.Slider(0, 10, value=3, step=0.1, label="Fade hình/nhạc đầu và cuối (giây)")
                    seamless = gr.Checkbox(value=True, label="Làm mượt điểm nối loop")
                    overlap = gr.Slider(0.1, 3, value=1, step=0.1, label="Thời lượng hòa điểm nối (giây)")
                    dust = gr.Checkbox(value=False, label="Thêm bụi bay (xuất lần đầu chậm hơn)")
                    duration_choice = gr.Radio(["30 giây", "125 giây", "Toàn bộ playlist"], value="30 giây", label="Thời lượng video nhạc")
                    mix_btn = gr.Button("Xuất video nhạc", variant="primary")
                cancel_btn = gr.Button("Hủy xuất video", variant="stop")
            with gr.Column():
                report = gr.Textbox(label="Trạng thái")
                comparison = gr.Image(label="Crop: Lanczos / Fallin")
                full = gr.Image(label="Frame đầy đủ (giao diện có thể thu nhỏ)")
                saved_path = gr.Textbox(label="File MP4 đã lưu trên máy", interactive=False)
                open_folder_btn = gr.Button("Mở thư mục kết quả")
                gr.Markdown("Video được ghi thẳng vào thư mục đã chọn. Không cần tải MP4 qua trình duyệt.")
        pick.click(first_frame, inputs=source, outputs=[ann, report], concurrency_id="studio", concurrency_limit=1)
        source.change(lambda: (None, "Hãy lấy frame và khoanh lại vùng watermark cho video mới."),
                      outputs=[ann, report], concurrency_id="studio", concurrency_limit=1)
        common = [source, ann, remove, variant, edge]
        preview_btn.click(preview, inputs=common+[grain, tile], outputs=[comparison, full, report], concurrency_id="studio", concurrency_limit=1)
        clip_event = export_btn.click(export_clip, inputs=common+[seconds, grain, tile, fps, output_dir], outputs=[saved_path, report], concurrency_id="studio", concurrency_limit=1)
        mix_event = mix_btn.click(export_mix, inputs=common+[seconds, grain, tile, fps, music, recursive, shuffle, seed, crossfade, fade, seamless, overlap, dust, duration_choice, output_dir],
                      outputs=[saved_path, report], concurrency_id="studio", concurrency_limit=1)
        cancel_btn.click(runner.cancel, outputs=report, queue=False, cancels=[clip_event, mix_event])
        folder_btn.click(pick_music, outputs=music, concurrency_id="folder-picker", concurrency_limit=1)
        output_folder_btn.click(pick_output, outputs=output_dir, concurrency_id="folder-picker", concurrency_limit=1)
        open_folder_btn.click(open_result, inputs=[saved_path, output_dir], outputs=report, queue=False)
        music_inputs = [music, recursive, shuffle, seed, crossfade]
        scan_btn.click(inspect_music, inputs=music_inputs, outputs=[playlist_table, playlist_summary])
        for control in music_inputs:
            control.change(inspect_music, inputs=music_inputs, outputs=[playlist_table, playlist_summary], trigger_mode="always_last")
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
        server_name=args.host, server_port=args.port, share=args.share, show_error=True, theme="soft")


if __name__ == "__main__":
    main()
