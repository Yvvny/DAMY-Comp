# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

binaries = []
hiddenimports = ['html', 'html.entities', 'html.parser', 'google.auth.exceptions', 'google.auth.transport.requests', 'google.oauth2.credentials', 'google_auth_oauthlib.flow', 'googleapiclient.discovery', 'googleapiclient.errors', 'win32com.client', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets']
binaries += collect_dynamic_libs('psycopg')
hiddenimports += collect_submodules('psycopg')
hiddenimports += collect_submodules('google.auth')
hiddenimports += collect_submodules('google.oauth2')
hiddenimports += collect_submodules('google_auth_oauthlib')
hiddenimports += collect_submodules('googleapiclient')
hiddenimports += collect_submodules('win32com')
hiddenimports += collect_submodules('folder_manager.calendar_import_v3')
hiddenimports += collect_submodules('folder_manager.order_import_v1')
hiddenimports += collect_submodules('folder_manager.qr_tags_v1')
hiddenimports += collect_submodules('folder_manager.proof_sorter')
hiddenimports += collect_submodules('folder_manager.proofing_online')
hiddenimports += collect_submodules('folder_manager.yearbook_online')
hiddenimports += collect_submodules('PySide6.QtPdf')
hiddenimports += collect_submodules('PySide6.QtPdfWidgets')


a = Analysis(
    ['run_folder_manager.py'],
    pathex=[],
    binaries=binaries,
    datas=[('folder_manager\\stages.json', 'folder_manager')],
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='DAMYComp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DAMYComp',
)
