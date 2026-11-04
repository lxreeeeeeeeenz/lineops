"""Garante as dependências do LineOps sem reinstalar à toa.

Guarda um carimbo (sha256 dos requirements) dentro do ambiente. Se os requirements não mudaram desde a última vez,
sai em um instante; se mudaram (versão nova trouxe pacote novo), instala só o que falta.
Uso: python tools/ensure_env.py requirements.txt [outro.txt ...]
"""
import hashlib
import pathlib
import subprocess
import sys

reqs = [pathlib.Path(p) for p in sys.argv[1:] if p]
h = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in reqs)).hexdigest()
stamp = pathlib.Path(sys.prefix) / "lineops-deps.sha256"
if stamp.exists() and stamp.read_text().strip() == h:
    print(" Dependencias ja instaladas.")
    sys.exit(0)
print(" Instalando ou atualizando dependencias (precisa de internet so agora)...")
cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
for p in reqs:
    cmd += ["-r", str(p)]
r = subprocess.call(cmd)
if r:
    sys.exit(r)
stamp.write_text(h)
print(" Dependencias prontas.")
