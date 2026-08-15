from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "run_mp3_downloader.bat"
SUPPORT_ALIAS = ROOT / "export_support.bat"


class LauncherContractTests(unittest.TestCase):
    def test_canonical_launcher_is_root_relative_and_forwards_arguments(self) -> None:
        text = CANONICAL.read_text(encoding="utf-8").replace("\r\n", "\n")
        lower = text.lower()
        self.assertIn('cd /d "%~dp0"', lower)
        self.assertIn("mp3_downloader.py", lower)
        self.assertIn('"%mp3_script%" %*', lower)
        local_venv = lower.index(r'.venv\scripts\python.exe')
        path_python = lower.index("where python.exe")
        py_launcher = lower.index("where py.exe")
        self.assertLess(local_venv, path_python)
        self.assertLess(path_python, py_launcher)
        self.assertNotIn("this launcher does not forward command-line arguments", lower)
        self.assertNotRegex(lower, r"sys\.version_info\s+\^>=")

    def test_explicit_arguments_are_dispatched_before_local_config_creation(self) -> None:
        text = CANONICAL.read_text(encoding="utf-8").replace("\r\n", "\n").lower()
        dispatch = text.index('if not "%~1"==""')
        config_copy = text.index('if not exist "%~dp0config.json"')
        self.assertLess(dispatch, config_copy)

    def test_support_launcher_is_only_a_thin_compatibility_redirect(self) -> None:
        text = SUPPORT_ALIAS.read_text(encoding="utf-8").replace("\r\n", "\n").lower()
        self.assertIn('call "%~dp0run_mp3_downloader.bat" --export-support %*', text)
        self.assertNotIn("where py", text)
        self.assertNotIn("import yt_dlp", text)
        self.assertNotIn("copy /y", text)

    def test_launchers_do_not_weaken_security_controls(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (CANONICAL, SUPPORT_ALIAS)
        ).lower()
        self.assertNotIn("executionpolicy bypass", combined)
        self.assertIsNone(re.search(r"(?:disable|exclude).{0,80}(?:defender|norton|smartscreen)", combined))


if __name__ == "__main__":
    unittest.main()
