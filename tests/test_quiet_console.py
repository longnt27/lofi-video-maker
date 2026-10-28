"""Exercise actual CLI + separate LaMa worker without downloading neural weights."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def call(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


@unittest.skipUnless(__import__('shutil').which('ffmpeg'), 'FFmpeg required')
class TestQuietPipelineCLI(unittest.TestCase):
    def test_model_warnings_go_to_log_but_milestones_remain_visible(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            stub = base / 'mock_packages'
            stub.mkdir()
            (stub / 'onnxruntime.py').write_text('''import sys
import numpy as np

def get_available_providers():
    return ['CoreMLExecutionProvider', 'CPUExecutionProvider']

def set_default_logger_severity(value):
    pass

class InferenceSession:
    def __init__(self, model_path, providers):
        assert providers == ['CPUExecutionProvider'], providers
        print('Context leak detected, CoreAnalytics returned false', file=sys.stderr)
        self._providers = providers
    def get_providers(self):
        return self._providers
    def run(self, _, inputs):
        print('Context leak detected, CoreAnalytics returned false', file=sys.stderr)
        return [np.full((1, 3, 512, 512), 128, dtype=np.float32)]
''')
            video = base / 'video.mp4'
            audio_dir = base / 'songs'
            audio_dir.mkdir()
            output = base / 'lofi.mp4'
            log = base / 'manual.log'
            model = base / 'mock.onnx'
            model.write_bytes(b'MOCK')
            r = call(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                      'testsrc2=size=128x80:rate=24:duration=0.5', '-c:v',
                      'libx264', '-pix_fmt', 'yuv420p', str(video)])
            self.assertEqual(r.returncode, 0, r.stderr)
            r = call(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                      'sine=frequency=330:duration=2.5', str(audio_dir / 'song1.wav')])
            self.assertEqual(r.returncode, 0, r.stderr)
            env = os.environ.copy()
            env['PYTHONPATH'] = str(stub) + os.pathsep + env.get('PYTHONPATH', '')
            r = call([sys.executable, str(ROOT / 'lofi_maker.py'),
                      '--video', str(video), '--music', str(audio_dir),
                      '--output', str(output), '--watermark-box', '8,8,12,12',
                      '--lama-model', str(model), '--lama-provider', 'cpu',
                      '--upscale', '2', '--upscale-engine', 'lanczos',
                      '--section-seconds', '1', '--dust-variants', '1',
                      '--dust-fps', '6', '--dust-count', '4', '--dust-fade', '.15',
                      '--fade', '.25', '--fps', '24', '--log-file', str(log),
                      '--cache-dir', str(base / 'cache')], env=env, timeout=120)
            self.assertEqual(r.returncode, 0, f'OUT:\n{r.stdout}\nERR:\n{r.stderr}\nLOG:\n{log.read_text() if log.exists() else "missing"}')
            self.assertIn('[1/4] Combine soundtrack', r.stdout)
            self.assertIn('[2/4] Prepare short source', r.stdout)
            self.assertIn('[3/4] Build reusable', r.stdout)
            self.assertIn('[4/4] Assemble final MP4', r.stdout)
            self.assertIn('LaMa inpaint: 12/12 frames (100%)', r.stdout)
            self.assertNotIn('Context leak detected', r.stdout)
            self.assertNotIn('Context leak detected', r.stderr)
            self.assertIn('Context leak detected', log.read_text())
            result = call(['ffprobe', '-v', 'error', '-show_entries',
                           'stream=width,height:format=duration', '-of', 'json', str(output)])
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(result.stdout)
            video_stream = next(s for s in info['streams'] if 'width' in s)
            self.assertEqual((video_stream['width'], video_stream['height']), (256,160))
            self.assertAlmostEqual(float(info['format']['duration']), 2.5, delta=0.15)

    def test_coreml_missing_fails_helpfully(self):
        sys.path.insert(0, str(ROOT))
        from video_preprocess import selected_lama_providers
        with self.assertRaisesRegex(RuntimeError, 'Use --lama-provider cpu'):
            selected_lama_providers(['CPUExecutionProvider'], 'coreml')
        self.assertEqual(selected_lama_providers(['CPUExecutionProvider','CoreMLExecutionProvider'], 'cpu'),
                         ['CPUExecutionProvider'])

if __name__ == '__main__':
    unittest.main()
