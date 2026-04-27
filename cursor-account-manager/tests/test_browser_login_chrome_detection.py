import unittest
from unittest.mock import patch

from cam import browser_login


class ChromeDetectionTests(unittest.TestCase):
    def test_windows_detects_standard_system_chrome_path(self):
        def fake_exists(path):
            return path == r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        with patch("os.path.exists", side_effect=fake_exists), \
             patch("shutil.which", return_value=None):
            self.assertTrue(browser_login._has_system_chrome("win32"))

    def test_windows_requires_system_chrome(self):
        with patch("os.path.exists", return_value=False), \
             patch("shutil.which", return_value=None):
            self.assertTrue(browser_login._requires_system_chrome("win32"))
            self.assertFalse(browser_login._has_system_chrome("win32"))


if __name__ == "__main__":
    unittest.main()
