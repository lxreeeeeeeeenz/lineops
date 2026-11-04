"""Voos de carga no RPL. Na GOL, os voos de carga (GOLLOG) costumam ter número 9xxx (GLO9xxx): por padrão ficam fora da
escala de passageiros e entram só se você marcar "Incluir voos de carga". As outras companhias não têm filtro (sem regra
informada)."""
from __future__ import annotations

import re

PATTERNS = {"GLO": re.compile(r"^(?:GLO|G3)9\d{3}$")}
LABELS = {"GLO": "GOL 9xxx"}


def applies(airline: str | None, include: bool | None) -> bool:
    """O filtro vale para esta geração? (companhia com regra e carga não incluída)"""
    return (airline or "").upper() in PATTERNS and not include


def is_cargo(airline: str | None, ident: str | None) -> bool:
    pat = PATTERNS.get((airline or "").upper())
    return bool(pat and ident and pat.match(ident.strip().upper()))


def label(airline: str | None) -> str | None:
    return LABELS.get((airline or "").upper())
