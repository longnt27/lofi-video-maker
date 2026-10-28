"""Lightweight smoke checks. No real neural model is required for the mock tests."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import video_preprocess as pre


def cmd(args):
    subprocess.run(args, check=True, capture_output=True, text=True)


@unittest.skipUnless(__import__('shutil').which('ffmpeg'), 'ffmpeg required')
class TestPreprocess(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.source = self.path / 'loop.mp4'
        cmd(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi',
             '-i', 'testsrc2=size=128x80:rate=24:duration=0.5',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.source)])
        self.fps = 24

    def test_box_validation(self):
        self.assertEqual(pre.parse_box('5, 10, 20, 30'), (5, 10, 20, 30))
        for invalid in ('5,-1,3,4', 'a,b,c,d', '1,2,0,2', '4,5'):
            with self.assertRaises(ValueError):
                pre.parse_box(invalid)

    def test_lanczos_preparation_and_cache(self):
        ns = types.SimpleNamespace(watermark_box=None, upscale=2, denoise=False,
            motion_interpolation=False, lama_model=None, cache_dir=self.path / 'cache',
            realesrgan_bin=None, upscale_engine='lanczos', fps=24,
            watermark_feather=3, watermark_grain=0.0,
            upscale_model='realesr-animevideov3', upscale_tile=128)
        result = pre.prepare_video(self.source, ns, self.path)
        self.assertEqual((pre.video_info(result)['width'], pre.video_info(result)['height']), (256, 160))
        self.assertEqual(pre.prepare_video(self.source, ns, self.path), result)

    def test_ai_upscale_external_binary(self):
        import cv2
        stub = self.path / 'fake-realesrgan-ncnn-vulkan'
        stub.write_text('''#!/usr/bin/env python3
import argparse
from pathlib import Path
import cv2
p=argparse.ArgumentParser()
for flag in ('-i','-o','-n','-s','-f','-t','-m'): p.add_argument(flag)
a=p.parse_args()
assert (Path(a.m) / (a.n + '-x' + a.s + '.param')).is_file()
assert (Path(a.m) / (a.n + '-x' + a.s + '.bin')).is_file()
Path(a.o).mkdir(exist_ok=True)
for input_file in sorted(Path(a.i).glob('frame_*.png')):
    img=cv2.imread(str(input_file))
    img=cv2.resize(img, None, fx=int(a.s), fy=int(a.s))
    cv2.imwrite(str(Path(a.o)/input_file.name), img)
''')
        stub.chmod(0o755)
        models = self.path / 'models'
        models.mkdir()
        for ext in ('.param', '.bin'):
            (models / ('realesr-animevideov3-x2' + ext)).write_bytes(b'mock')
        ns = types.SimpleNamespace(watermark_box=None, upscale=2, denoise=False,
            motion_interpolation=False, lama_model=None, cache_dir=self.path / 'cache',
            realesrgan_bin=stub, upscale_engine='ai', fps=24,
            watermark_feather=3, watermark_grain=0.0,
            upscale_model='realesr-animevideov3', upscale_tile=128)
        result = pre.prepare_video(self.source, ns, self.path)
        self.assertEqual((pre.video_info(result)['width'], pre.video_info(result)['height']), (256, 160))

    def test_lama_patching_with_mock_model(self):
        try:
            import numpy as np
            import cv2
        except ImportError:
            self.skipTest('OpenCV/numpy unavailable')
        class Session:
            def __init__(self, path, providers):
                self._providers = providers
            def get_providers(self): return self._providers
            def run(self, _unused, inputs):
                self.assert_shape(inputs)
                # Simulate an inpainted RGB image from a model with output 0..255.
                return [np.full((1, 3, 512, 512), 128.0, dtype=np.float32)]
            def assert_shape(self, inputs):
                assert inputs['image'].shape == (1, 3, 512, 512)
                assert inputs['mask'].shape == (1, 1, 512, 512)
        stub = types.ModuleType('onnxruntime')
        stub.get_available_providers = lambda: ['CPUExecutionProvider']
        stub.InferenceSession = Session
        previous = sys.modules.get('onnxruntime')
        sys.modules['onnxruntime'] = stub
        self.addCleanup(lambda: sys.modules.pop('onnxruntime', None) if previous is None
                        else sys.modules.__setitem__('onnxruntime', previous))
        model = self.path / 'fake.onnx'
        model.write_bytes(b'fake model')
        output = self.path / 'inpainted.mp4'
        pre.inpaint_video(self.source, output, (8,8,12,12), model, 24, 2, 0)
        self.assertEqual(pre.video_info(output)['frames'], 12)
        cap = cv2.VideoCapture(str(output))
        ok, frame = cap.read()
        cap.release()
        self.assertTrue(ok)
        # An inpainted patch should approach the mocked output gray.
        self.assertLess(abs(int(frame[14,14,0]) - 128), 25)

    def test_both_ai_stage_handoffs(self):
        """Run LaMa preprocessing then AI upscale in one call with fake engines."""
        import numpy as np
        from unittest.mock import patch
        # Fake LaMa produces a constant repaired region; no network model needed.
        model = self.path / 'fake-lama.onnx'
        model.write_bytes(b'mock model')
        stub = self.path / 'fake-ncnn'
        stub.write_text('''#!/usr/bin/env python3
import argparse
from pathlib import Path
import cv2
p=argparse.ArgumentParser()
for opt in ('-i','-o','-n','-s','-f','-t','-m'):p.add_argument(opt)
a=p.parse_args()
Path(a.o).mkdir(exist_ok=True)
for f in Path(a.i).glob('frame_*.png'):
    image=cv2.imread(str(f))
    cv2.imwrite(str(Path(a.o)/f.name),cv2.resize(image,None,fx=int(a.s),fy=int(a.s)))
''')
        stub.chmod(0o755)
        models = self.path / 'models'
        models.mkdir()
        for ext in ('.param', '.bin'):
            (models / ('realesr-animevideov3-x2' + ext)).write_bytes(b'mock')
        ns = types.SimpleNamespace(watermark_box=(8, 8, 12, 12), upscale=2,
            denoise=False, motion_interpolation=False, lama_model=model,
            cache_dir=self.path / 'cache', realesrgan_bin=stub,
            upscale_engine='ai', fps=24, watermark_feather=2,
            watermark_grain=0.0, upscale_model='realesr-animevideov3', upscale_tile=128,
            inpaint_in_process=True)
        with patch.object(pre, 'make_inpainter', return_value=lambda crop, mask: np.full_like(crop, 128)):
            result = pre.prepare_video(self.source, ns, self.path)
        self.assertEqual((pre.video_info(result)['width'], pre.video_info(result)['height']), (256, 160))
        self.assertGreater(result.stat().st_size, 1000)

    def test_find_existing_binary_in_other_project_bin_and_models(self):
        project = self.path / 'other_project'
        folder = project / 'bin'
        models = folder / 'models'
        models.mkdir(parents=True)
        binary = folder / 'realesrgan-ncnn-vulkan'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        for ext in ('.bin', '.param'):
            (models / f'realesr-animevideov3-x2{ext}').write_bytes(b'model')
        resolved = pre.find_existing_upscaler(root=project, model='realesr-animevideov3', scale=2)
        self.assertEqual(resolved.binary, binary.resolve())
        self.assertEqual(resolved.models, models.resolve())

        # Also auto-discover from a separate lo-fi folder next to other_project.
        new_project = self.path / 'lofi_project'
        new_project.mkdir()
        with patch.object(pre.Path, 'cwd', return_value=new_project):
            resolved2 = pre.find_existing_upscaler(scale=2)
        self.assertEqual(resolved2.binary, binary.resolve())

    def test_upscaler_does_not_silently_ignore_missing_models(self):
        project = self.path / 'other_project'
        folder = project / 'bin'
        folder.mkdir(parents=True)
        binary = folder / 'realesrgan-ncnn-vulkan'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        with self.assertRaisesRegex(FileNotFoundError, 'missing model files'):
            pre.find_existing_upscaler(root=project, scale=2)

    def test_upscaler_cache_key_changes_with_weights(self):
        project = self.path / 'other_project'
        folder = project / 'bin'
        models = folder / 'models'
        models.mkdir(parents=True)
        binary = folder / 'realesrgan-ncnn-vulkan'
        binary.write_text('''#!/usr/bin/env python3
import argparse
from pathlib import Path
import cv2
p=argparse.ArgumentParser()
for flag in ('-i','-o','-n','-s','-f','-t','-m'):p.add_argument(flag)
a=p.parse_args()
Path(a.o).mkdir(exist_ok=True)
for path in Path(a.i).glob('frame_*.png'):
    image=cv2.imread(str(path))
    cv2.imwrite(str(Path(a.o)/path.name),cv2.resize(image,None,fx=int(a.s),fy=int(a.s)))
''')
        binary.chmod(0o755)
        param = models / 'realesr-animevideov3-x2.param'
        weights = models / 'realesr-animevideov3-x2.bin'
        param.write_bytes(b'mock-param')
        weights.write_bytes(b'mock-weights')
        ns = types.SimpleNamespace(watermark_box=None, upscale=2, denoise=False,
            motion_interpolation=False, lama_model=None, cache_dir=self.path / 'cache',
            realesrgan_bin=None, realesrgan_root=project, realesrgan_model_dir=None,
            upscale_engine='ai', fps=24,
            watermark_feather=3, watermark_grain=0.0,
            upscale_model='realesr-animevideov3', upscale_tile=128)
        first = pre.prepare_video(self.source, ns, self.path)
        self.assertEqual(pre.prepare_video(self.source, ns, self.path), first)
        weights.write_bytes(b'mock-weights-v2')
        second = pre.prepare_video(self.source, ns, self.path)
        self.assertNotEqual(second, first)

if __name__ == '__main__':
    unittest.main(verbosity=2)

class TestWatermarkPickerWindowCleanup(unittest.TestCase):
    """No GUI needed: verify close is dispatched before ROI returns."""

    def test_enter_closes_picker_and_pumps_events(self):
        events = []
        cv2_mock = types.ModuleType('cv2')
        cv2_mock.error = type('CvError', (Exception,), {})
        def select(*args, **kwargs):
            events.append('select')
            return (10, 20, 30, 40)
        def destroy(*args):
            events.append('destroy')
        def wait(*args):
            events.append('wait')
        cv2_mock.selectROI = select
        cv2_mock.destroyWindow = destroy
        cv2_mock.waitKey = wait
        with patch.dict(sys.modules, {'cv2': cv2_mock}), \
             patch.object(pre, 'extract_first_frame', return_value=object()):
            self.assertEqual(pre.pick_watermark_box(Path('unused.mp4')), (10,20,30,40))
        self.assertEqual(events, ['select', 'destroy', 'wait', 'wait', 'wait', 'wait'])

    def test_cancel_still_closes_picker(self):
        events = []
        cv2_mock = types.ModuleType('cv2')
        cv2_mock.error = type('CvError', (Exception,), {})
        cv2_mock.selectROI = lambda *a, **k: (0, 0, 0, 0)
        cv2_mock.destroyWindow = lambda *a: events.append('destroy')
        cv2_mock.waitKey = lambda *a: events.append('wait')
        with patch.dict(sys.modules, {'cv2': cv2_mock}), \
             patch.object(pre, 'extract_first_frame', return_value=object()):
            with self.assertRaisesRegex(ValueError, 'No watermark area selected'):
                pre.pick_watermark_box(Path('unused.mp4'))
        self.assertEqual(events, ['destroy', 'wait', 'wait', 'wait', 'wait'])
