"""Atualiza o recurso de versão do .exe com a versão do lineops/__init__.py (chamado pelo build.bat)."""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import lineops  # noqa: E402

v = lineops.__version__
nums = (re.findall(r"\d+", v) + ["0"] * 4)[:4]          # 3.0.2b4 → 3, 0, 2, 4
p = pathlib.Path(__file__).with_name("version_info.txt")
s = p.read_text(encoding="utf-8")
s = re.sub(r"filevers=\([^)]*\)", "filevers=(%s)" % ", ".join(nums), s)
s = re.sub(r"prodvers=\([^)]*\)", "prodvers=(%s)" % ", ".join(nums), s)
s = re.sub(r"StringStruct\('(FileVersion|ProductVersion)', '[^']*'\)", lambda m: f"StringStruct('{m.group(1)}', '{v}')", s)
p.write_text(s, encoding="utf-8")
# a tela pede os scripts com ?v=<versão>: depois de atualizar, o navegador não mistura tela nova com script velho
idx = pathlib.Path(__file__).resolve().parents[1] / "lineops" / "web" / "index.html"
h = idx.read_text(encoding="utf-8")
idx.write_text(re.sub(r'((?:css/app\.css|js/ui\.js|js/app\.js))(\?v=[^"]*)?"', lambda m: f'{m.group(1)}?v={v}"', h), encoding="utf-8")
print(v)
