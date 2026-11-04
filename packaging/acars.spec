# PyInstaller: LineOps ACARS.exe (console, só biblioteca padrão). Rode pelo build.bat.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
a = Analysis([os.path.join(SPECPATH, "launch_acars.py")], pathex=[os.path.join(ROOT, "acars")], hiddenimports=["lineops_acars.interactive"],
             excludes=["tkinter", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LineOps ACARS", console=True, icon=os.path.join(SPECPATH, "lineops.ico"), upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="LineOps ACARS", upx=False)
