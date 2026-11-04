"""O Planner do AeroScale com os ajustes da LineOps, sem mexer na cópia literal do motor (lineops/aeroscale).

Uma subclasse do scheduler.Planner entra no lugar dele só durante a geração (planner_for), com um lock:
* PERNOITE PEDIDO: no dia da noite pedida, o aeroporto pedido vira o must_end do próprio AeroScale (Planner.plan_day:
  +10 000 para a sequência de voos reais que termina lá). O dia e o seguinte ficam de trabalho; uma folga regulamentar ou
  um sobreaviso ali troca de dia com um dia de trabalho próximo, sem passar do máximo de dias seguidos.
* CARGA: voos de carga (GOL: GLO9xxx) saem do sorteio, a não ser que você marque “Incluir voos de carga”.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date, timedelta, timezone

from . import cargo
from .pernoite import FIXED_OFF, sleep_at


def _make_planner_class(base_cls):
    from .aeroscale import crew, crew_profiles
    from .aeroscale.airports import get_tz, to_local

    class LineOpsPlanner(base_cls):
        """O Planner do AeroScale com dois ajustes da LineOps: pernoite pedido (must_end no dia da noite pedida) e voos de
        carga fora do sorteio (a não ser que você peça)."""

        def build_pool(self, d1, d2):
            super().build_pool(d1, d2)
            if self.p.get("include_cargo"):
                return
            cut = 0
            for ap in list(self._occ_by_adep):
                keep = [o for o in self._occ_by_adep[ap] if not cargo.is_cargo(self.p["airline"], o.get("ident"))]
                cut += len(self._occ_by_adep[ap]) - len(keep)
                self._occ_by_adep[ap] = keep
            if cut:
                self._occ_deps = {k: [o["dep"] for o in v] for k, v in self._occ_by_adep.items()}
                self.diag("info", "carga", f"{cut} ocorrência(s) de voos de carga ({cargo.label(self.p['airline'])}) ficaram fora do "
                                           "sorteio. Para voar carga, marque “Incluir voos de carga” no gerador.")

        def _nights(self) -> dict:
            return {date.fromisoformat(r["night"]): r["icao"] for r in (self.p.get("overnights") or [])}

        def _make_room(self, dp: dict) -> None:
            nights = self._nights()
            self._night_conflict = {}
            max_c = int(self.p.get("max_consecutive_days") or 6)
            protect = set()
            for d in nights:
                protect |= {d, d + timedelta(days=1)}

            def max_run():
                run = best = 0
                for x in sorted(dp):
                    run = 0 if dp[x][0] == "off" else run + 1
                    best = max(best, run)
                return best

            for d in sorted(nights):
                for x in (d, d + timedelta(days=1)):
                    if x not in dp or dp[x][0] == "work":
                        continue
                    kind, reason = dp[x][0], dp[x][1]
                    if kind == "reserve":
                        self._night_conflict[d] = f"{x:%d/%m} é dia de reserva"
                        break
                    if reason in FIXED_OFF:
                        self._night_conflict[d] = f"{x:%d/%m} é {reason}"
                        break
                    moved = False
                    for k in (2, -1, 3, -2, 4, -3, 5, -4):
                        e = x + timedelta(days=k)
                        if e not in dp or e in protect or dp[e][0] != "work":
                            continue
                        dp[x], dp[e] = dp[e], dp[x]
                        if not (self.p.get("crew_days_off") and self.crew_on()) or max_run() <= max_c:
                            self.diag("info", "pernoite_folga", f"{'Sobreaviso' if kind == 'sobreaviso' else 'Folga regulamentar'} de {x:%d/%m} passou para {e:%d/%m} "
                                                                f"para caber o pernoite pedido na noite de {d:%d/%m}.", x.isoformat())
                            moved = True
                            break
                        dp[x], dp[e] = dp[e], dp[x]
                    if not moved:
                        self._night_conflict[d] = (f"{'o sobreaviso' if kind == 'sobreaviso' else 'a folga regulamentar'} de {x:%d/%m} não pode mudar de dia sem passar de "
                                                   f"{max_c} dias seguidos de trabalho")
                        break

        def inactive_today(self, d, loc, base, had_legs):
            nights = self._nights()
            if d in nights or (d - timedelta(days=1)) in nights:
                return False                                            # o dia do pernoite pedido é de voo
            return super().inactive_today(d, loc, base, had_legs)

        def plan_day(self, d, loc, t_avail, base, final_day, must_end=None, end_limit=None, **kw):
            want = self._nights().get(d)
            if want and not must_end and d not in getattr(self, "_night_conflict", {}):
                day = super().plan_day(d, loc, t_avail, base, final_day, want, end_limit, **kw)
                if day:
                    return day
                # nenhuma sequência termina no aeroporto pedido: o dia segue normal (o pedido sai como não atendido)
            return super().plan_day(d, loc, t_avail, base, final_day, must_end, end_limit, **kw)

        def plan(self, loc, t_avail, d_from, d_to, base, had_legs, must_end=None, end_limit=None):
            nights = self._nights()
            if not nights:
                return super().plan(loc, t_avail, d_from, d_to, base, had_legs, must_end, end_limit)
            orig = crew.plan_days_off

            def wrapped(*a, **k):
                dp = orig(*a, **k)
                self._make_room(dp)
                return dp
            crew.plan_days_off = wrapped
            try:
                legs = super().plan(loc, t_avail, d_from, d_to, base, had_legs, must_end, end_limit)
            finally:
                crew.plan_days_off = orig
            view = []
            for o in legs:
                lt = to_local(o["dep"], get_tz(self.tz(o["adep"])) or timezone.utc)
                view.append({"ld": lt.date(), "hour": lt.hour, "dep": o["dep"], "arr": o["arr"], "adep": o["adep"],
                             "ades": o["ades"], "ident": o.get("ident")})
            for d, want in sorted(nights.items()):
                at, last = sleep_at(view, d, loc)
                ok = bool(at) and crew_profiles.same_city(self.p["airline"], at, want)
                if ok:
                    arr = to_local(last["arr"], get_tz(self.tz(last["ades"])) or timezone.utc) if last else None
                    self.diag("info", "pernoite_pedido_ok", f"Pernoite pedido na noite de {d:%d/%m} em {want}: encaixado"
                              + (f" (chega no {last['ident'] or 'voo'} às {arr:%H:%M} local em {last['ades']})" if last else "")
                              + ".", d.isoformat())
                else:
                    why = self._night_conflict.get(d) or (
                        f"nenhuma sequência de voos reais do RPL, dentro das regras, termina o dia em {want} "
                        f"(o dia terminou em {at or '—'})")
                    self.diag("warning", "pernoite_pedido_falhou", f"Pernoite pedido na noite de {d:%d/%m} em {want}: não deu, "
                              f"{why}.", d.isoformat())
            return legs

    return LineOpsPlanner


_PATCH_LOCK = threading.RLock()


@contextmanager
def planner_for(params: dict):
    """Enquanto gera, o scheduler do AeroScale usa o Planner da LineOps (pernoite pedido e filtro de carga)."""
    from .aeroscale import scheduler
    if not params.get("overnights") and not cargo.applies(params.get("airline"), params.get("include_cargo")):
        yield
        return
    with _PATCH_LOCK:
        orig = scheduler.Planner
        scheduler.Planner = _make_planner_class(orig)
        try:
            yield
        finally:
            scheduler.Planner = orig
