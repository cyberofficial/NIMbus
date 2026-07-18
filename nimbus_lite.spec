# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['packaged_entry.py'],
    pathex=[],
    binaries=[],
    datas=[('build_resources\\tiktoken_cache\\9b5ad71b2ce5302211f9c61530b329a4922fc6a4', 'tiktoken_cache')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.http.httptools_impl', 'uvicorn.protocols.http.flow_control', 'uvicorn.protocols.websockets.auto', 'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.middleware.asgi2', 'uvicorn.middleware.wsgi', 'uvicorn.middleware.proxy_headers', 'tiktoken_ext.openai_public', 'config', 'config.settings', 'config.nim', 'config.logging_config', 'api', 'api.app', 'api.routes', 'api.dependencies', 'api.middleware', 'api.models', 'api.models.anthropic', 'api.models.responses', 'api.bot_protection', 'api.optimization_handlers', 'api.request_utils', 'providers', 'providers.provider', 'providers.base', 'providers.text', 'providers.rate_limit', 'providers.request_queue', 'providers.logging_utils', 'providers.error_mapping', 'providers.sse_builder', 'providers.header_capture', 'providers.heuristic_tool_parser', 'providers.request', 'providers.tiktoken_cache', 'cli', 'cli.manager', 'cli.session', 'cli.process_registry', 'discord_bot', 'discord_bot.bot', 'discord_bot.cog', 'discord_bot.conversation', 'discord_bot.persistence', 'discord_bot.rate_limit', 'discord_bot.views', 'discord_bot.user_blocking', 'discord_bot.tools', 'discord_bot.tools.web_search', 'websearch', 'websearch.duckduckgo_html', 'api.swapper', 'api.swapper.parser', 'api.swapper.validator'],
    hookspath=['build_hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'unittest', 'IPython', 'jupyter', 'sentry_sdk', 'pygments', 'rich', 'jinja2', 'PyYAML', 'email_validator', 'torch', 'grpcio', 'PIL', 'matplotlib', 'tkinter', 'PyNaCl', 'PyQt5', 'PySide6', 'playwright', 'playwright.async_api', 'playwright._impl', 'playwright_stealth', 'greenlet', 'greenlet._greenlet', 'pyee', 'pyee.asyncio'],
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
    name='nimbus_lite',
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
