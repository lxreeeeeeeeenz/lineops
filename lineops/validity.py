"""Dados com prazo de validade: o LineOps avisa quando vencem, em vez de calcular com regra velha sem dizer.

* CCT dos aeronautas (salários): vigência impressa na própria convenção.
* Tabelas de INSS e IRRF: valem para um ano-calendário.
* Módulos de regra do AeroScale: cópia de uma versão; o backup importado pode ser de outra.
* Backup do LineOps: sem cópia recente, um disco que morre leva tudo.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

SOON_DAYS = 30


def _status(until: date, today: date) -> str:
    if today > until:
        return "vencido"
    if (until - today).days <= SOON_DAYS:
        return "vencendo"
    return "ok"


def check(conn, backups_dir: Path | None = None, today: date | None = None) -> list[dict]:
    from . import payroll
    from .aeroscale import SOURCE_VERSION
    today = today or datetime.now(timezone.utc).date()
    out = []
    pay_on = payroll.enabled(conn)
    cct_to = date.fromisoformat(payroll.CCT["valid_to"])
    st = _status(cct_to, today)
    out.append({"key": "cct", "label": payroll.CCT["label"], "until": cct_to.isoformat(), "status": st, "relevant": pay_on,
                "action": "A convenção nova muda pisos, hora e diárias: quando sair, atualize o contrato em Salários "
                          "(os valores que você digitar ficam marcados como seus)."})
    tax_to = date(payroll.TAX_YEAR, 12, 31)
    out.append({"key": "impostos", "label": f"Tabelas de INSS e IRRF de {payroll.TAX_YEAR}", "until": tax_to.isoformat(),
                "status": _status(tax_to, today), "relevant": pay_on,
                "action": "Em janeiro saem as tabelas novas: até lá, o contracheque avisa que usa as de " + str(payroll.TAX_YEAR) + "."})
    last = conn.execute("SELECT source_version, imported_at FROM import_batch ORDER BY id DESC LIMIT 1").fetchone()
    if last and last["source_version"] and str(last["source_version"]) != SOURCE_VERSION:
        out.append({"key": "aeroscale", "label": f"Regras do AeroScale: LineOps tem a {SOURCE_VERSION}, o backup é da {last['source_version']}",
                    "until": None, "status": "vencendo", "relevant": True,
                    "action": "Copie os módulos novos do AeroScale para lineops/aeroscale (ou instale a versão do LineOps que já traz)."})
    if backups_dir is not None:
        files = sorted(f for pat in ("lineops-backup-*.zip", "gol" "ops-backup-*.zip", "flygolv-backup-*.zip") for f in Path(backups_dir).glob(pat)) \
            if Path(backups_dir).exists() else []
        has_data = conn.execute("SELECT COUNT(*) FROM flight").fetchone()[0] or conn.execute("SELECT COUNT(*) FROM roster_leg").fetchone()[0]
        if has_data:
            age = (today - datetime.fromtimestamp(files[-1].stat().st_mtime, timezone.utc).date()).days if files else None
            st = "vencido" if age is None or age > 14 else ("vencendo" if age > 7 else "ok")
            out.append({"key": "backup", "label": "Backup do LineOps" + (f": o último tem {age} dia(s)" if age is not None else ": nenhum feito ainda"),
                        "until": None, "status": st, "relevant": True,
                        "action": "Crie um em Ajustes → Backups (ou pela bandeja do app) e guarde uma cópia fora deste computador."})
    return out
