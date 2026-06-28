"""Runtime hook to configure Playwright browser paths for the bundled exe."""
import os
import sys

# Browsers are embedded via --add-data and extracted to _MEIPASS at runtime
if getattr(sys, "frozen", False):
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        browsers_dir = os.path.join(meipass, "ms-playwright")
        if os.path.isdir(browsers_dir):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_dir
