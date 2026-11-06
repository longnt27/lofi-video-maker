"""Studio uploads and render temp stay under the chosen local storage root."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lofi_studio import DEFAULT_HOME, configure_temp_storage

ROOT = Path(__file__).resolve().parents[1]


class StudioStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-storage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ)
        cached = patch.object(tempfile, "tempdir", tempfile.tempdir)
        env.start()
        cached.start()
        self.addCleanup(env.stop)
        self.addCleanup(cached.stop)

    def test_config_moves_new_temp_and_leaves_existing_files(self):
        old = self.root/"old-system-temp"
        old.mkdir()
        sentinel = old/"keep-this-upload.mp4"
        sentinel.write_bytes(b"existing user file")
        os.environ.update(TEMP=str(old), TMP=str(old), TMPDIR=str(old),
                          GRADIO_TEMP_DIR=str(old))
        tempfile.tempdir = str(old)  # Simulate Python having cached the C: default.
        home = configure_temp_storage(self.root/"chosen-drive"/"studio")
        self.assertEqual(Path(os.environ["GRADIO_TEMP_DIR"]), home/"uploads")
        for name in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(Path(os.environ[name]), home/"tmp")
        self.assertEqual(Path(tempfile.gettempdir()), home/"tmp")
        with tempfile.NamedTemporaryFile() as generated:
            self.assertEqual(Path(generated.name).parent, home/"tmp")
        self.assertEqual(sentinel.read_bytes(), b"existing user file")

    def test_default_is_relative_to_script_not_shell_working_directory(self):
        self.assertEqual(DEFAULT_HOME, ROOT/"cache"/"studio")
        before = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(DEFAULT_HOME, ROOT/"cache"/"studio")
        finally:
            os.chdir(before)

    @unittest.skipUnless(importlib.util.find_spec("gradio"), "Gradio required")
    def test_fresh_gradio_process_uses_selected_root_and_worker_inherits_it(self):
        # Exercise a non-canonical spelling on every OS, in addition to macOS
        # /var aliases and Windows short names supplied by TemporaryDirectory.
        (self.root/"path-alias").mkdir()
        home = self.root/"path-alias"/".."/"custom-storage"
        code = (
            "import json,os,subprocess,sys,tempfile;"
            f"sys.path.insert(0,{str(ROOT)!r});"
            "from pathlib import Path;"
            "from lofi_studio import build_app;"
            f"app=build_app(Path({str(home)!r}),'cpu');"
            "from gradio.utils import get_upload_folder;"
            "print(json.dumps({'upload':get_upload_folder(),"
            "'temp':tempfile.gettempdir(),"
            "'child_temp':subprocess.check_output([sys.executable,'-c',"
            "'import tempfile; print(tempfile.gettempdir())'],text=True).strip()}))"
        )
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.root,
            env={**os.environ, "GRADIO_ANALYTICS_ENABLED": "False"},
            capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        # Compare existing filesystem locations, including the drive/device,
        # rather than spellings (symlinks, Windows 8.3 names, or '..'). Missing
        # directories and a fallback to a different temp root must still fail.
        for key, folder in (("upload", "uploads"), ("temp", "tmp"),
                            ("child_temp", "tmp")):
            with self.subTest(location=key):
                actual, expected = Path(data[key]), home/folder
                self.assertTrue(actual.samefile(expected),
                                f"{key}: {actual} is not {expected}")


if __name__ == "__main__":
    unittest.main()

