import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("playwright")
datas += collect_data_files("browser_cookie3")
datas += [("README.md", "."), (".env.example", ".")]

hiddenimports = collect_submodules("playwright")
hiddenimports += collect_submodules("browser_cookie3")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ZhihuScraper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="ZhihuScraper.app",
        icon=None,
        bundle_identifier="com.andycao.zhihuscraper",
    )
