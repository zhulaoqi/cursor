import unittest
from pathlib import Path


class WindowsSetupPatchrightInstallTests(unittest.TestCase):
    def test_windows_setup_installs_patchright_browser_binary(self):
        script = Path(__file__).resolve().parents[1] / "windows-setup.ps1"
        text = script.read_text(encoding="utf-8")

        self.assertIn("-m patchright install chromium", text)


if __name__ == "__main__":
    unittest.main()
