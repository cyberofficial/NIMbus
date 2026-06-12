"""Download the cl100k_base tiktoken encoding and save with SHA1 cache naming.

This is called by build_exe.bat to avoid quoting issues with inline Python.
"""

import hashlib
import os
import urllib.request

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "build_resources",
    "tiktoken_cache",
)

URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
CACHE_KEY = hashlib.sha1(URL.encode()).hexdigest()  # 9b5ad71b2ce5302211f9c61530b329a4922fc6a4
DEST = os.path.join(CACHE_DIR, CACHE_KEY)

os.makedirs(CACHE_DIR, exist_ok=True)

if os.path.exists(DEST):
    print(f"  Already cached: {DEST}")
else:
    print(f"  Downloading {URL}...")
    urllib.request.urlretrieve(URL, DEST)
    size = os.path.getsize(DEST)
    print(f"  Downloaded {size / 1024:.1f} KB to {DEST}")
