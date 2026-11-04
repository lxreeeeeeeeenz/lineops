"""Ponto de entrada do LineOps ACARS.exe (PyInstaller). Dois cliques abrem o modo guiado."""
import sys

from lineops_acars.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
