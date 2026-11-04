"""Ponto de entrada do LineOps.exe (PyInstaller)."""
import multiprocessing
import sys

from lineops.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
