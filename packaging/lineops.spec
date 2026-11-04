# PyInstaller: LineOps.exe (app com janela, sem console). Rode pelo build.bat.
# onedir (pasta) em vez de onefile: abre mais rápido e o antivírus implica menos.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(ROOT, "lineops", "web"), "lineops/web"),
    (os.path.join(ROOT, "lineops", "migrations"), "lineops/migrations"),
    (os.path.join(ROOT, "lineops", "assets"), "lineops/assets"),                       # foto de perfil fixa (cmdt079)
    (os.path.join(ROOT, "lineops", "aeroscale", "data"), "lineops/aeroscale/data"),
    (os.path.join(ROOT, "lineops", "aeroscale", "rpl"), "lineops/aeroscale/rpl"),       # RPL do CGNA embutidos (motor de escalas)
]
binaries, hidden = [], []
for pkg in ("webview", "pystray"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
datas += collect_data_files("reportlab") + collect_data_files("tzdata")
hidden += collect_submodules("uvicorn") + collect_submodules("lineops") + ["tzdata", "clr", "PIL._tkinter_finder"]

a = Analysis([os.path.join(SPECPATH, "launch_lineops.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["tkinter", "pytest", "httpx"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LineOps", console=False, icon=os.path.join(SPECPATH, "lineops.ico"),
          version=os.path.join(SPECPATH, "version_info.txt"), upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="LineOps", upx=False)
