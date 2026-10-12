import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import launcher  # noqa: E402


class LauncherTest(unittest.TestCase):
    def test_windows_entries_are_ascii(self):
        for name in ("start.bat", "update.bat"):
            with self.subTest(name=name):
                (ROOT / name).read_bytes().decode("ascii")

    def test_missing_dependencies_install_from_tsinghua_mirror(self):
        with patch("launcher.importlib.util.find_spec", return_value=None), \
                patch("launcher.subprocess.run",
                      return_value=CompletedProcess([], 0)) as run:
            self.assertTrue(launcher.install_dependencies())
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(command[-2:], ["-i", launcher.MIRROR])

    def test_failed_install_stops_launch(self):
        with patch("launcher.importlib.util.find_spec", return_value=None), \
                patch("launcher.port_open", return_value=False), \
                patch("launcher.subprocess.run",
                      return_value=CompletedProcess([], 1)) as run, \
                patch("launcher.webbrowser.open") as browser:
            self.assertEqual(launcher.main(), 1)
        run.assert_called_once()
        browser.assert_not_called()
