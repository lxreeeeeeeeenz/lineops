"""Frota de referência: as listas de matrícula e configuração que você mandou (nov/2026), sem nada acrescentado.

O texto vai aqui do jeito que chegou (só a formatação de linha) e é lido por um parser: cada valor da configuração guarda a
fonte "lista informada pelo usuário (nov/2026)". O que a lista não diz fica em branco (desconhecido), com uma exceção
explícita: na GOL, quando a linha não cita SFP, o campo fica "não consta na lista" (não é afirmação de que não tem).
* LATAM (carreira TAM): A319, A320 e A320neo. "ANALOGIC" = standby analógico; ISIS = standby integrado (ISIS).
  PS-LH* foram classificados como A320neo porque vieram na lista "neos latam".
* GOL (carreira GLO): B738, FMC (Collins/Honeywell), empuxo (22K a 27K), SFP e standby (ISFD/analógico).
  PS-GF* (cargueiros) ficaram de fora, como você pediu.
Entra uma vez por versão da lista: matrícula que já existe na sua frota não é recriada, e só ganha os campos que ainda
estavam vazios (o que você editou nunca é sobrescrito). Aeronave que você apagou ou desativou não volta sozinha.
"""
from __future__ import annotations

import json
import re

from .db import now_iso, one, tx

VERSION = "2026-11-04"
SOURCE = "lista informada pelo usuário (nov/2026)"
KEY = "frota_ref_versao"

LATAM_A320 = """
PR-MYP - ISIS - DDRMI - NO DCDU
PR-MYQ - ISIS - DDRMI - NO DCDU
PR-MYR - ISIS - DDRMI - NO DCDU
PR-MYT - ISIS - DDRMI - NO DCDU
PR-MYV - ISIS - DDRMI - NO DCDU
PR-MYW - ISIS - DDRMI - NO DCDU
PR-MYX - ISIS - DDRMI - NO DCDU
PR-MYY - ISIS - DDRMI - NO DCDU
PR-MYZ - ISIS - DDRMI - NO DCDU
PR-TQB - ISIS - NO DDRMI - DCDU
PR-TQC - ISIS - NO DDRMI - DCDU
PR-TYA - ISIS - DDRMI - NO DCDU
PR-TYD - ISIS - DDRMI - NO DCDU
PR-TYF - ISIS - DDRMI - NO DCDU
PR-TYH - ISIS - DDRMI - NO DCDU
PR-TYI - ISIS - DDRMI - NO DCDU
PR-TYJ - ISIS - DDRMI - NO DCDU
PR-TYK - ISIS - DDRMI - NO DCDU
PR-TYL - ISIS - DDRMI - NO DCDU
PR-TYM - ISIS - DDRMI - NO DCDU
PR-TYN - ISIS - DDRMI - NO DCDU
PR-TYO - ISIS - DDRMI - NO DCDU
PR-TYP - ISIS - DDRMI - NO DCDU
PR-TYQ - ISIS - DDRMI - NO DCDU
PR-TYR - ISIS - DDRMI - NO DCDU
PR-TYS - ISIS - DDRMI - NO DCDU
PR-TYT - ISIS - DDRMI - NO DCDU
PR-TYU - ISIS - DDRMI - NO DCDU
PR-TYV - ISIS - DDRMI - NO DCDU
PR-MAG - ANALOGIC - DDRMI - NO DCDU
PR-MAK - ANALOGIC - DDRMI - NO DCDU
PR-MBA - ISIS - DDRMI - NO DCDU
PR-MBF - ISIS - DDRMI - NO DCDU
PR-MBG - ANALOGIC - DDRMI - NO DCDU
PR-MBH - ISIS - DDRMI - NO DCDU
PR-MHA - ISIS - DDRMI - NO DCDU
PR-MHE - ISIS - DDRMI - NO DCDU
PR-MHF - ISIS - DDRMI - NO DCDU
PR-MHG - ISIS - DDRMI - NO DCDU
PR-MHI - ISIS - DDRMI - NO DCDU
PR-MHJ - ISIS - DDRMI - NO DCDU
PR-MHK - ISIS - DDRMI - NO DCDU
PR-MHM - ISIS - DDRMI - NO DCDU
PR-MHP - ISIS - DDRMI - NO DCDU
PR-MHQ - ISIS - DDRMI - NO DCDU
PR-MHR - ISIS - DDRMI - NO DCDU
PR-MHU - ISIS - DDRMI - NO DCDU
PR-MHW - ISIS - DDRMI - NO DCDU
PR-MHX - ISIS - DDRMI - NO DCDU
PR-MHZ - ISIS - DDRMI - NO DCDU
PR-MYA - ISIS - DDRMI - NO DCDU
PR-MYH - ISIS - DDRMI - NO DCDU
PR-MYI - ISIS - DDRMI - NO DCDU
PR-MYJ - ISIS - DDRMI - NO DCDU
PR-MYK - ISIS - DDRMI - NO DCDU
PR-MYN - ISIS - DDRMI - DCDU
PR-MYO - ISIS - DDRMI - DCDU
"""

LATAM_A20N = """
PR-XBA ISIS - NO ROW/ROPS - NO DCDU - NO DRAMS
PR-XBB ISIS - NO ROW/ROPS - NO DCDU - NO DRAMS
PR-XBC ISIS - NO ROW/ROPS - NO DCDU - NO DRAMS
PR-XBD ISIS - NO ROW/ROPS - NO DCDU - NO DRAMS
PR-XBE ISIS - NO ROW/ROPS - NO DCDU - NO DRAMS
PR-XBF ISIS - NO ROW/ROPS - NO DCDU - NO DRAMS
PR-XBG ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBH ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBI ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBJ ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBK ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBL ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBM ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBN ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBO ISIS - ROW/ROPS - NO DCDU - NO DRAMS
PR-XBP ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBQ ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBR ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBS ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBT ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBU ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBV ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBW ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBX ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBY ISIS - ROW/ROPS - NO DCDU - DRAMS
PR-XBZ ISIS - ROW/ROPS - NO DCDU - DRAMS
PS-LHA NO INFO
PS-LHB NO INFO
PS-LHC ISIS - ROW/ROPS - DCDU - DRAMS
PS-LHD ISIS ROW/ROPS- DRAIMS
PS-LHE NO INFO
PS-LHF NO INFO
PS-LHG NO INFO
PS-LHI NO INFO
PS-LHM ISIS ROW/ROPS- DRAIMS NO DCDU
PS-LHO ISIS ROW/ROPS- DRAIMS NO DCDU
PS-LHP ISIS - ROW/ROPS - NO DCDU - DRAMS
PS-LHQ ISIS ROW/ROPS- DRAIMS
"""

LATAM_A319 = """
PR-MBN - ISIS - DDRMI - NO DCDU
PR-MBU - ISIS - DDRMI - NO DCDU
PR-MBV - ISIS - DDRMI - NO DCDU
PR-MBW - ISIS - DDRMI - NO DCDU
PR-MYL - ISIS - DDRMI - NO DCDU
PR-MYM - ISIS - DDRMI - NO DCDU
PT-TMA - ISIS - DDRMI - NO DCDU
PT-TMB - ISIS - DDRMI - NO DCDU
PT-TMC - ISIS - DDRMI - NO DCDU
PT-TMD - ISIS - DDRMI - NO DCDU
PT-TME - ISIS - DDRMI - NO DCDU
PT-TMG - ISIS - DDRMI - NO DCDU
PT-TMI - ISIS - DDRMI - NO DCDU
PT-TML - ISIS - DDRMI - NO DCDU
PT-TMO - ISIS - DDRMI - NO DCDU
PT-TMT - ANALOGIC - DDRMI - NO DCDU
PT-TPA - ISIS - NO DDRMI - NO DCDU
PT-TPB - ISIS - NO DDRMI - NO DCDU
"""

GOL_B738 = """
PR-GEA – Collins (22K) ANALOGIC
PR-GEC – Collins (22K) ISFD
PR-GED – Collins (22K) ISFD
PR-GEH – Collins (22K) ANALOGIC
PR-GEI – Honeywell (22K) ANALOGIC
PR-GEJ – Collins (22K) ANALOGIC
PR-GEK – Honeywell (22K) ANALOGIC
PR-GEQ – Collins (22K) ISFD
PR-GGE – Honeywell (27K) SFP Analogic
PR-GGF – Collins (27K) SFP Analogic
PR-GGH – Honeywell (24K) SFP Analogic*
PR-GGL – Collins (26K) SFP Analogic
PR-GGM – Honeywell (26K) SFP Analogic
PR-GGP – Honeywell (27K) SFP Analogic
PR-GGQ – Collins (27K) SFP 2.0 Analogic*
PR-GGR – Collins (27K) SFP Analogic
PR-GIH – Collins (22K) ANALOGIC
PR-GOQ – Honeywell (22K) ANALOGIC
PR-GOR – Collins (22K) ANALOGIC
PR-GTC – Collins (27K) SFP Analogic*
PR-GTE – Honeywell (27K) SFP Analogic
PR-GTG – Honeywell (27K) SFP Analogic
PR-GTH – Honeywell (26K) SFP Analogic*
PR-GTL – Honeywell (24K) SFP Analogic*
PR-GTM – Honeywell (26K) SFP Analogic
PR-GUB – Collins (27K) SFP 2.0 ISFD
PR-GUC – Collins (26K) SFP ISFD
PR-GUE – Collins (26K) SFP 2.0 ISFD
PR-GUH – Collins (27K) SFP ISFD
PR-GUI – Collins (27K) SFP ISFD
PR-GUJ – Collins (27K) SFP ISFD
PR-GUK – Honeywell (27K) SFP ISFD
PR-GUL – Collins (26K) SFP ISFD
PR-GUM – Collins (27K) SFP 2.0 ISFD
PR-GUN – Collins (27K) SFP 2.0 ISFD
PR-GUP – Collins (26K) SFP ISFD
PR-GUR – Honeywell (27K) ISFD
PR-GUT – Collins (26K) SFP ISFD
PR-GUU – Collins (27K) SFP 2.0 ISFD
PR-GUV – Collins (27K) SFP 2.0 ISFD
PR-GUX – Collins (27K) SFP 2.0 ISFD
PR-GUY – Collins (27K) SFP 2.0 ISFD
PR-GUZ – Collins (27K) SFP 2.0 ISFD
PR-GXA – Collins (27K) SFP 2.0 ISFD
PR-GXB – Collins (27K) SFP 2.0 ISFD
PR-GXC – Collins (27K) SFP 2.0 ISFD
PR-GXD – Honeywell (27K) SFP 2.0 ISFD
PR-GXE – Collins (27K) SFP 2.0 ISFD
PR-GXF – Collins (27K) SFP 2.0 ISFD
PR-GXH – Collins (27K) SFP 2.0 ISFD
PR-GXI – Collins (26K) SFP 2.0 ISFD
PR-GXJ – Honeywell (27K) SFP 2.0 ISFD
PR-GXL – Collins (26K) SFP 2.0 ISFD
PR-GXM – Collins (27K) SFP 3.0 ISFD
PR-GXN – Collins (27K) SFP 3.0 ISFD
PR-GXP – Collins (27K) SFP 3.0 ISFD
PR-GXQ – Collins (27K) SFP 3.0 ISFD
PR-GXR – Collins (27K) SFP 3.0 ISFD
PR-GXT – Collins (27K) SFP 3.0 ISFD
PR-GXU – Collins (27K) SFP 3.0 ISFD
PR-GXW – Collins (27K) SFP 3.0 ISFD
PR-GXX – Collins (27K) SFP 3.0 ISFD
PR-GYA – Collins (27K) SFP 3.0 ISFD (NEW CUTOFF)
PR-GYD – Collins (27K) SFP 3.0 ISFD (NEW CUTOFF / LED)
PR-GZH – Collins (26K) ISFD
PR-GZI – Collins (26K) ISFD
PR-GZS – Collins (24K) ISFD
PR-GZU – Collins (24K) ISFD
PR-VBF – Collins (27K) SFP ANALOGIC
PR-VBG – Collins (27K) SFP ANALOGIC
PR-VBQ – Honeywell (22K) ANALOGIC
PR-GKA – Collins (26K) Analogic
PR-GKB – Collins (26K) Analogic
PR-GKC – Collins (26K) Analogic
"""

# campos da configuração (chave → rótulo na tela)
LABELS = {"standby": "Standby", "ddrmi": "DDRMI", "dcdu": "DCDU", "row_rops": "ROW/ROPS", "drams": "DRAMS",
          "fmc": "FMC", "thrust": "Empuxo (CFM56-7B)", "sfp": "SFP", "remark": "Observação da lista"}
REG = re.compile(r"^(P[PRSTU]-[A-Z]{3})\b\s*[-–]?\s*(.*)$")


def _val(v):
    return {"value": v, "source": SOURCE}


def _parse_latam(line: str) -> tuple[str, dict, str | None]:
    m = REG.match(line.strip())
    reg, rest = m.group(1), m.group(2).upper()
    if "NO INFO" in rest:
        return reg, {}, "sem informação de configuração na lista"
    cfg = {}
    for m2 in re.finditer(r"(NO\s+)?(ISIS|ANALOGIC|DDRMI|DCDU|ROW/ROPS|DRAMS|DRAIMS)", rest):
        neg, name = bool(m2.group(1)), m2.group(2).replace("DRAIMS", "DRAMS")
        if name in ("ISIS", "ANALOGIC"):
            cfg["standby"] = _val("ISIS" if name == "ISIS" else "Analógico")
        else:
            cfg[{"DDRMI": "ddrmi", "DCDU": "dcdu", "ROW/ROPS": "row_rops", "DRAMS": "drams"}[name]] = _val("NÃO" if neg else "SIM")
    return reg, cfg, None


def _parse_gol(line: str) -> tuple[str, dict, str | None]:
    m = REG.match(line.strip())
    reg, rest = m.group(1), m.group(2)
    cfg = {}
    fm = re.match(r"(Collins|Honeywell)\s*\(([^)]*)\)\s*(.*)$", rest)
    fmc, thrust, tail = fm.group(1), fm.group(2).strip(), fm.group(3)
    cfg["fmc"] = _val(fmc)
    cfg["thrust"] = _val(thrust)
    notes = []
    extra = re.search(r"\(([^)]*)\)\s*$", tail)
    if extra:
        notes.append(extra.group(1).strip())
        tail = tail[:extra.start()]
    if tail.strip().endswith("*"):
        notes.append("marcado com * na lista")
        tail = tail.strip()[:-1]
    sfp = re.search(r"SFP(?:\s+(\d\.\d))?", tail, re.I)
    cfg["sfp"] = _val(f"SFP {sfp.group(1)}" if sfp and sfp.group(1) else "SFP" if sfp else "não consta na lista")
    if re.search(r"ISFD", tail, re.I):
        cfg["standby"] = _val("ISFD")
    elif re.search(r"ANALOGIC", tail, re.I):
        cfg["standby"] = _val("Analógico")
    if notes:
        cfg["remark"] = _val("; ".join(notes))
    return reg, cfg, None


def records() -> list[dict]:
    out = []
    for airline, type_icao, model, text, parse in (("TAM", "A320", "Airbus A320", LATAM_A320, _parse_latam),
                                                   ("TAM", "A20N", "Airbus A320neo", LATAM_A20N, _parse_latam),
                                                   ("TAM", "A319", "Airbus A319", LATAM_A319, _parse_latam),
                                                   ("GLO", "B738", "Boeing 737-800", GOL_B738, _parse_gol)):
        for line in text.strip().splitlines():
            if not line.strip():
                continue
            reg, cfg, note = parse(line)
            out.append({"registration": reg, "airline": airline, "type_icao": type_icao, "model_name": model, "config": cfg,
                        "note": note, "raw": line.strip()})
    return out


def ensure(conn, airline: str) -> dict:
    """Coloca a lista na frota desta carreira, uma vez por versão. Devolve o que mudou."""
    airline = (airline or "").upper()
    cur = one(conn.execute("SELECT value FROM setting WHERE key=?", (KEY,)))
    if cur and cur["value"] == VERSION:
        return {"added": 0, "filled": 0, "skipped": True}
    recs = [r for r in records() if r["airline"] == airline]
    added = filled = 0
    now = now_iso()
    with tx(conn):
        for r in recs:
            row = one(conn.execute("SELECT id, config, type_icao FROM aircraft WHERE registration=?", (r["registration"],)))
            if row:
                cfg = json.loads(row["config"] or "{}")
                new = {k: v for k, v in r["config"].items() if k not in cfg}
                if new:
                    cfg.update(new)
                    conn.execute("UPDATE aircraft SET config=?, updated_at=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), now, row["id"]))
                    filled += 1
                continue
            notes = f"Da {SOURCE}: “{r['raw']}”." + (f" {r['note'].capitalize()}." if r["note"] else "")
            conn.execute("""INSERT INTO aircraft(registration, airline, type_icao, model_name, config, limits, source, source_ref, notes,
                            created_at, updated_at) VALUES (?,?,?,?,?, '{}', 'MANUAL', ?, ?, ?, ?)""",
                         (r["registration"], airline, r["type_icao"], r["model_name"], json.dumps(r["config"], ensure_ascii=False),
                          f"frota_ref:{VERSION}", notes, now, now))
            added += 1
        conn.execute("INSERT INTO setting(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, VERSION))
    return {"added": added, "filled": filled, "skipped": False}
