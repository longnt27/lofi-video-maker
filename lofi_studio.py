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
            return None, "Chọn video để bắt đầu."
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
        # Show the repaired region at output resolution, rather than hiding it
        # in a central crop unrelated to the selected watermark.
        if box:
            bx, by, bw, bh = box
            cx = (bx+bw/2)*size[0]/info["width"]
            cy = (by+bh/2)*size[1]/info["height"]
            x = max(0, min(size[0]-sz, round(cx-sz/2)))
            y = max(0, min(size[1]-sz, round(cy-sz/2)))
        else:
            x, y = (size[0]-sz)//2, (size[1]-sz)//2
        comp = np.concatenate([basic[y:y+sz, x:x+sz], up[y:y+sz, x:x+sz]], axis=1)
        return cv2.cvtColor(comp, cv2.COLOR_BGR2RGB), cv2.cvtColor(up, cv2.COLOR_BGR2RGB), (
            f"{size[0]}×{size[1]} | {time.perf_counter()-start:.1f}s/frame (lần đầu gồm tải model). "
            "Vùng khoanh: trái = gốc phóng lớn; phải = LaMa + Fallin. Nếu còn vệt, khoanh sát logo và xem lại trước khi xuất.")

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

    def export_mix(path, ann, remove, variant, edge, seconds, grain, tile, fps,
                   music, recursive, shuffle, seed, crossfade, fade, seamless, overlap, dust,
                   duration_choice, output_dir, keep_source_audio, source_audio_volume,
                   progress=gr.Progress()):
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
            dust=dust, preview=preview_seconds, codec="h264", output_dir=output_dir,
            keep_source_audio=bool(keep_source_audio), source_audio_volume=source_audio_volume)
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

    with gr.Blocks(title="LoFi Studio") as app:
        gr.Markdown("## LoFi Studio\nVideo gốc → Xóa watermark → Upscale → Loop + nhạc → **Một bản mix MP4**")
        with gr.Row():
            with gr.Column():
                gr.Markdown("### 1. Video và watermark")
                source = gr.File(label="Video gốc", file_types=["video"], type="filepath")
                remove = gr.Checkbox(value=True, label="Xóa watermark bằng LaMa")
                ann = image_annotator(label="Khoanh sát logo, chừa 1–2 pixel quanh nét chữ; tránh khoanh cả mảng nền", image_type="numpy", single_box=True)
                gr.Markdown("Frame đầu hiện tự động khi chọn video. LaMa tự vẽ lại vùng bị che nên có thể mất chi tiết hoặc nhấp nháy ở nền phức tạp.")
                with gr.Accordion("Chất lượng và thiết lập nâng cao", open=False):
                    variant = gr.Radio(["soft", "strong"], value="soft", label="Upscale Fallin: Soft tự nhiên; Strong sắc hơn")
                    edge = gr.Dropdown(["Native AI 2x", "1080", "1440", "2160"], value="Native AI 2x", label="Cạnh ngắn đầu ra; tối đa AI 2×")
                    seconds = gr.Slider(1, 60, value=10, step=1, label="Số giây đầu dùng làm clip nền")
                    fps = gr.Dropdown(["source", "24", "25", "30", "50", "60"], value="source", label="FPS: source = giữ nguồn")
                    grain = gr.Slider(0, 5, value=0, step=0.1, label="Grain vùng sửa (không khôi phục chi tiết)")
                    tile = gr.Checkbox(value=True, label="Chia ô upscale để giảm VRAM")
                    recursive = gr.Checkbox(value=False, label="Lấy nhạc trong thư mục con")
                    shuffle = gr.Checkbox(value=False, label="Xáo trộn bài")
                    seed = gr.Number(value=42, precision=0, label="Seed xáo trộn")
                    crossfade = gr.Slider(0, 10, value=2, step=0.1, label="Crossfade giữa bài (giây)")
                    fade = gr.Slider(0, 10, value=3, step=0.1, label="Fade đầu/cuối (giây)")
                    seamless = gr.Checkbox(value=True, label="Làm mượt điểm nối loop")
                    overlap = gr.Slider(0.1, 3, value=1, step=0.1, label="Hòa điểm nối (giây)")
                    dust = gr.Checkbox(value=False, label="Thêm bụi bay (chậm hơn)")
                preview_btn = gr.Button("Xem thử vùng watermark + upscale")
                gr.Markdown("### 2. Nhạc")
                folder_btn = gr.Button("Chọn thư mục nhạc")
                music = gr.Textbox(label="Thư mục nhạc", placeholder=r"D:\Music\LoFi")
                with gr.Accordion("Danh sách nhạc", open=False):
                    scan_btn = gr.Button("Đọc lại danh sách")
                    playlist_table = gr.Dataframe(headers=["STT", "Bài nhạc", "Thời lượng"], datatype=["number", "str", "str"], interactive=False)
                playlist_summary = gr.Textbox(label="Playlist", interactive=False)
                keep_source_audio = gr.Checkbox(value=True, label="Giữ tiếng môi trường từ video gốc")
                source_audio_volume = gr.Slider(0, 100, value=10, step=1, label="Âm lượng tiếng gốc (%)")
                gr.Markdown("Tiếng gốc được hòa nhẹ khi lặp và trộn dưới nhạc. Nếu có lời nói hoặc nhạc trong video gốc, chúng cũng được giữ. Video không có tiếng sẽ chỉ dùng playlist.")
                gr.Markdown("### 3. Lưu bản mix")
                output_folder_btn = gr.Button("Chọn thư mục lưu")
                output_dir = gr.Textbox(value=str(home/"outputs"), label="Thư mục lưu trên máy")
                duration_choice = gr.Radio(["30 giây", "125 giây", "Toàn bộ playlist"], value="Toàn bộ playlist", label="Thời lượng (chọn 30 giây để thử)")
                gr.Markdown("Mỗi lần bấm xuất chỉ lưu **một bản mix mới** vào thư mục này. Clip xử lý và file tạm không xuất vào đây. Các bản mix từ lần chạy trước vẫn được giữ.")
                mix_btn = gr.Button("Tạo bản mix MP4", variant="primary")
                cancel_btn = gr.Button("Hủy", variant="stop")
            with gr.Column():
                report = gr.Textbox(label="Trạng thái", interactive=False)
                comparison = gr.Image(label="Vùng watermark: gốc / đã xử lý")
                full = gr.Image(label="Toàn khung hình sau xử lý")
                saved_path = gr.Textbox(label="Bản mix đã lưu", interactive=False)
                open_folder_btn = gr.Button("Mở thư mục kết quả")
                gr.Markdown("Lưu thẳng trên máy, không cần tải về. Fallin: renarchi, CC BY 4.0.")
        source.change(first_frame, inputs=source, outputs=[ann, report], concurrency_id="studio", concurrency_limit=1)
        common = [source, ann, remove, variant, edge]
        preview_btn.click(preview, inputs=common+[grain, tile], outputs=[comparison, full, report], concurrency_id="studio", concurrency_limit=1)
        mix_event = mix_btn.click(export_mix, inputs=common+[seconds, grain, tile, fps, music, recursive, shuffle, seed, crossfade, fade, seamless, overlap, dust, duration_choice, output_dir, keep_source_audio, source_audio_volume],
                      outputs=[saved_path, report], concurrency_id="studio", concurrency_limit=1)
        cancel_btn.click(runner.cancel, outputs=report, queue=False, cancels=[mix_event])
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
