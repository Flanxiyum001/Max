from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import prepare_native_desktop as launcher


class DesktopSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.config = self.project / "pyproject.toml"
        self.config.write_text("[project]\nversion = '1'\n", encoding="utf-8")
        self.stamp = self.project / ".max-desktop-installed"

    def test_first_install_and_warm_start(self) -> None:
        success = subprocess.CompletedProcess([], 0)
        with (
            patch.object(launcher, "desktop_present", return_value=True),
            patch.object(launcher.subprocess, "run", return_value=success) as run,
        ):
            self.assertEqual(launcher.prepare(self.project, self.stamp), 0)
            self.assertEqual(launcher.prepare(self.project, self.stamp), 0)
            run.assert_called_once()
            self.assertEqual(launcher.read_stamp(self.stamp), launcher.fingerprint(self.project))

    def test_changed_project_or_missing_desktop_reinstalls(self) -> None:
        launcher.write_stamp(self.stamp, launcher.fingerprint(self.project))
        for installed in (True, False):
            self.config.write_text("[project]\nversion = '2'\n", encoding="utf-8")
            with (
                patch.object(launcher, "desktop_present", return_value=installed),
                patch.object(
                    launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
                ) as run,
            ):
                self.assertEqual(launcher.prepare(self.project, self.stamp), 0)
                run.assert_called_once()

    def test_failed_repair_leaves_no_success_stamp(self) -> None:
        launcher.write_stamp(self.stamp, launcher.fingerprint(self.project))
        with patch.object(
            launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 9)
        ) as run:
            self.assertEqual(launcher.prepare(self.project, self.stamp, repair=True), 9)
            self.assertIn("--force-reinstall", run.call_args.args[0])
            self.assertFalse(self.stamp.exists())


if __name__ == "__main__":
    unittest.main()
