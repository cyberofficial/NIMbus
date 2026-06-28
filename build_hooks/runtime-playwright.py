"""Runtime hook to configure Playwright browser paths for the bundled exe."""
import os
import sys

# Set browsers path relative to the exe directory
if getattr(sys, "frozen", False):
    exe_dir = os.path.dirname(sys.executable)
    browsers_dir = os.path.join(exe_dir, "ms-playwright")
    if os.path.isdir(browsers_dir):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_dir
