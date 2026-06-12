# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['packaged_entry.py'],
    pathex=[],
    binaries=[],
    datas=[('build_resources\\tiktoken_cache\\9b5ad71b2ce5302211f9c61530b329a4922fc6a4', 'tiktoken_cache')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.http.httptools_impl', 'uvicorn.protocols.http.flow_control', 'uvicorn.protocols.websockets.auto', 'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.middleware.asgi2', 'uvicorn.middleware.wsgi', 'uvicorn.middleware.proxy_headers', 'tiktoken_ext.openai_public'],
    hookspath=['build_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'unittest', 'IPython', 'jupyter', 'sentry_sdk', 'pygments', 'rich', 'jinja2', 'PyYAML', 'email_validator', 'torch', 'grpcio', 'PIL', 'matplotlib', 'tkinter', 'PyNaCl', 'PyQt5', 'PySide6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='nimbus',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
