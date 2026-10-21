# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from scripts.build_mermaid import check_assets

check_assets()

web_datas, web_binaries, web_imports = collect_all('tkinterweb')
tk_datas, tk_binaries, tk_imports = collect_all('tkinterweb_tkhtml')
ctk_datas, ctk_binaries, ctk_imports = collect_all('customtkinter')


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=web_binaries + tk_binaries + ctk_binaries,
    datas=[('assets/mermaid', 'assets/mermaid'), ('rules', 'rules'), ('사용설명.md', '.'), ('AI_규칙작성_가이드.md', '.'), ('후처리_지원범위.md', '.'), ('아키텍처_및_규칙엔진.md', '.'), ('THIRD_PARTY_NOTICES.txt', '.')] + web_datas + tk_datas + ctk_datas,
    hiddenimports=web_imports + tk_imports + ctk_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='LabelPorter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
