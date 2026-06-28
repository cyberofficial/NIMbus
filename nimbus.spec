# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['packaged_entry.py'],
    pathex=[],
    binaries=[],
    datas=[('build_resources\\tiktoken_cache\\9b5ad71b2ce5302211f9c61530b329a4922fc6a4', 'tiktoken_cache'), ('build_resources\\ms-playwright\\chromium-1223', 'ms-playwright/chromium-1223'), ('build_resources\\ms-playwright\\chromium_headless_shell-1223', 'ms-playwright/chromium_headless_shell-1223'), ('build_resources\\ms-playwright\\ffmpeg-1011', 'ms-playwright/ffmpeg-1011'), ('build_resources\\ms-playwright\\winldd-1007', 'ms-playwright/winldd-1007'), ('build_resources\\ms-playwright\\.links', 'ms-playwright/.links'), ('venv\\Lib\\site-packages\\playwright', 'playwright'), ('venv\\Lib\\site-packages\\playwright_stealth', 'playwright_stealth'), ('venv\\Lib\\site-packages\\greenlet', 'greenlet'), ('venv\\Lib\\site-packages\\pyee', 'pyee'), ('venv\\Lib\\site-packages\\playwright\\driver\\node.exe', 'playwright/driver'), ('venv\\Lib\\site-packages\\playwright\\driver\\package', 'playwright/driver/package')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.http.httptools_impl', 'uvicorn.protocols.http.flow_control', 'uvicorn.protocols.websockets.auto', 'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.middleware.asgi2', 'uvicorn.middleware.wsgi', 'uvicorn.middleware.proxy_headers', 'tiktoken_ext.openai_public', 'playwright', 'playwright.async_api', 'playwright._impl._browser_type', 'playwright_stealth', 'greenlet', 'greenlet._greenlet', 'pyee', 'pyee.asyncio', 'config', 'config.settings', 'config.nim', 'config.logging_config', 'api', 'api.app', 'api.routes', 'api.dependencies', 'api.middleware', 'api.models', 'api.models.anthropic', 'api.models.responses', 'api.bot_protection', 'api.optimization_handlers', 'api.request_utils', 'providers', 'providers.provider', 'providers.base', 'providers.text', 'providers.rate_limit', 'providers.request_queue', 'providers.logging_utils', 'providers.error_mapping', 'providers.sse_builder', 'providers.header_capture', 'providers.heuristic_tool_parser', 'providers.request', 'providers.tiktoken_cache', 'cli', 'cli.manager', 'cli.session', 'cli.process_registry', 'discord_bot', 'discord_bot.bot', 'discord_bot.cog', 'discord_bot.conversation', 'discord_bot.persistence', 'discord_bot.rate_limit', 'discord_bot.views', 'discord_bot.user_blocking', 'discord_bot.tools', 'discord_bot.tools.web_search', 'websearch', 'websearch.duckduckgo_html', 'api.swapper', 'api.swapper.parser', 'api.swapper.validator'],
    hookspath=['build_hooks'],
    hooksconfig={},
    runtime_hooks=['build_hooks/runtime-playwright.py'],
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
