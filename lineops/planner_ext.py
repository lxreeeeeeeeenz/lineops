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


# o mapa de continuações tem centenas de KB: nos parâmetros da escala (que o AeroScale grava inteiros) vai só uma ficha
_FOLLOW: dict = {}
_FOLLOW_LOCK = threading.Lock()


def follow_stash(m: dict) -> str:
    import uuid
    k = "cont:" + uuid.uuid4().hex[:10]
    with _FOLLOW_LOCK:
        _FOLLOW[k] = m
    return k


def follow_get(v) -> dict:
    if isinstance(v, str):
        return _FOLLOW.get(v) or {}
    return v or {}


def follow_drop(v):
    if isinstance(v, str):
        with _FOLLOW_LOCK:
            _FOLLOW.pop(v, None)


def _make_planner_class(base_cls):
    from .aeroscale import crew, crew_profiles
    from .aeroscale.airports import get_tz, to_local

    class LineOpsPlanner(base_cls):
        """O Planner do AeroScale com dois ajustes da LineOps: pernoite pedido (must_end no dia da noite pedida) e voos de
        carga fora do sorteio (a não ser que você peça)."""

        # ---- continuações (3.0.2 beta): depois de X vem Y, se Y existir naquele dia e couber
        def _follow(self) -> dict:
            return follow_get(self.p.get("follow"))

        def _cont_targets(self, ap: str, y: str) -> list:
            """As ocorrências do pool que saem de ap e servem como continuação y (voo ou @destino), em ordem de partida."""
            cache = self.__dict__.setdefault("_lo_targets", {})
            k = (ap, y)
            if k not in cache:
                cache[k] = [o for o in self._occ_by_adep.get(ap, [])
                            if (y.startswith("@") and o["ades"] == y[1:]) or o.get("ident") == y]
            return cache[k]

        def _cont_entry(self, a: dict):
            """A continuação que vale para esta ocorrência de a (já com o sorteio da força), ou None."""
            from .continuacao import entry, roll
            rule = self._follow().get(a.get("ident") or "")
            e = entry(rule.get(str(a["dep"].weekday()))) if rule else None
            if not e:
                return None
            if e["p"] < 1:
                rolls = self.__dict__.setdefault("_lo_rolls", {})
                k = (a.get("ident"), a["dep"].date())
                if k not in rolls:
                    rolls[k] = roll(self.p.get("seed"), a.get("ident") or "", a["dep"].date())
                if rolls[k] >= e["p"]:
                    return None                            # identificada, mas a força sorteou "livre" desta vez
            return e

        def _must_next(self, a: dict) -> set:
            """As ocorrências que podem vir depois de a (vazio = sem regra para hoje). A regra é um voo (TAM3262) ou um
            destino (@SBPL: qualquer voo para Petrolina); vale só se existir de verdade no RPL, saindo do destino de a
            entre 25 min e 4 h depois do calço."""
            from .continuacao import GAP_MAX, GAP_MIN
            e = self._cont_entry(a)
            if not e:
                return set()
            lo, hi = a["arr"] + timedelta(minutes=GAP_MIN), a["arr"] + timedelta(minutes=GAP_MAX)
            taken = getattr(self, "taken", set())
            return {o["ref"] for o in self._cont_targets(a["ades"], e["to"]) if lo <= o["dep"] <= hi and o["ref"] not in taken}

        def chain_ok(self, chain) -> bool:
            if self._follow():
                for a, b in zip(chain, chain[1:]):
                    ok = self._must_next(a)
                    if ok and b.get("ref") not in ok:
                        return False                       # depois de X só pode vir a continuação
            return super().chain_ok(chain)

        def _pick(self, finals):
            need = getattr(self, "_cur_min", None)
            if need:
                ok = [f for f in finals if len([o for o in f[0] if not o.get("deadhead")]) >= need]
                if ok:                                     # só vale o dia com o mínimo de pernas pedido (se existir)
                    finals = ok
            if self._follow():
                ok = [f for f in finals if not (f[0] and self._must_next(f[0][-1]))]
                if ok:                                     # o dia não termina em X quando Y existe (o avião segue)
                    finals = ok
            return super()._pick(finals)

        def ops(self):
            """Faz a continuação obrigatória entrar primeiro na busca (como um sucessor muito visto no ADS-B)."""
            o = super().ops()
            if not self._follow() or getattr(o, "_lineops_follow", False):
                return o
            base_flight = o.flight
            planner = self

            from .aeroscale.predict import OBS_MIN_N
            from .continuacao import entry
            def flight(ident, adep, ades):
                memo = planner.__dict__.setdefault("_lo_memo", {})
                k0 = (ident, adep, ades)
                if k0 in memo:
                    return memo[k0]
                f = base_flight(ident, adep, ades)
                rule = planner._follow().get(ident or "")
                if rule:
                    # como um sucessor visto no ADS-B: a sua regra conta como "sempre visto" (n de n), a identificada
                    # como p × n. Nunca acima de n: o modelo de rodízio do AeroScale usa a fração vezes/n.
                    n = max(int((f or {}).get("n") or 0), OBS_MIN_N)
                    extra = {}
                    for v in rule.values():
                        e = entry(v)
                        w = n if e["src"] == "regra" else max(1, min(n, round(e["p"] * n)))
                        for x in planner._cont_targets(ades, e["to"]):
                            k = f"{x.get('ident')}:{x['adep']}-{x['ades']}"
                            extra[k] = max(extra.get(k, 0), w)
                    if extra:
                        g = dict(f or {"n": 0, "night": 0, "first": 0, "next": {}})
                        nxt = dict(g.get("next") or {})
                        for k, w in extra.items():
                            nxt[k] = max(int(nxt.get(k, 0)), w)
                        g["n"], g["next"] = n, nxt
                        f = g
                memo[k0] = f
                return f
            o.flight = flight
            o._lineops_follow = True
            return o

        def build_pool(self, d1, d2):
            super().build_pool(d1, d2)
            self.__dict__.pop("_lo_targets", None)
            self.__dict__.pop("_lo_memo", None)
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

        # ---- mínimo de pernas em dias escolhidos (3.0.3): o dia é de voo e, entre as opções, só valem as com n ≥ mínimo
        def _mins(self) -> dict:
            return {date.fromisoformat(k): int(v) for k, v in (self.p.get("day_legs_min") or {}).items()}

        def _force_work(self, dp: dict, x, protect: set, why: str, max_c: int):
            """Troca a folga/sobreaviso de x por um dia de trabalho próximo. Devolve None (ok) ou o motivo de não dar."""
            if x not in dp or dp[x][0] == "work":
                return None
            kind, reason = dp[x][0], dp[x][1]
            if kind == "reserve":
                return f"{x:%d/%m} é dia de reserva"
            if reason in FIXED_OFF:
                return f"{x:%d/%m} é {reason}"

            def max_run():
                run = best = 0
                for q in sorted(dp):
                    run = 0 if dp[q][0] == "off" else run + 1
                    best = max(best, run)
                return best
            for k in (2, -1, 3, -2, 4, -3, 5, -4):
                e = x + timedelta(days=k)
                if e not in dp or e in protect or dp[e][0] != "work":
                    continue
                dp[x], dp[e] = dp[e], dp[x]
                if not (self.p.get("crew_days_off") and self.crew_on()) or max_run() <= max_c:
                    self.diag("info", "pernoite_folga", f"{'Sobreaviso' if kind == 'sobreaviso' else 'Folga regulamentar'} de {x:%d/%m} passou para {e:%d/%m} "
                                                        f"para caber {why}.", x.isoformat())
                    return None
                dp[x], dp[e] = dp[e], dp[x]
            return (f"{'o sobreaviso' if kind == 'sobreaviso' else 'a folga regulamentar'} de {x:%d/%m} não pode mudar de dia sem passar de "
                    f"{max_c} dias seguidos de trabalho")

        def _make_room(self, dp: dict) -> None:
            nights, mins = self._nights(), self._mins()
            self._night_conflict, self._min_conflict = {}, {}
            max_c = int(self.p.get("max_consecutive_days") or 6)
            protect = set(mins)
            for d in nights:
                protect |= {d, d + timedelta(days=1)}
            for d in sorted(nights):
                for x in (d, d + timedelta(days=1)):
                    why = self._force_work(dp, x, protect, f"o pernoite pedido na noite de {d:%d/%m}", max_c)
                    if why:
                        self._night_conflict[d] = why
                        break
            for d in sorted(mins):
                why = self._force_work(dp, d, protect, f"o mínimo de {mins[d]} perna(s) pedido para {d:%d/%m}", max_c)
                if why:
                    self._min_conflict[d] = why

        def inactive_today(self, d, loc, base, had_legs):
            nights = self._nights()
            if d in nights or (d - timedelta(days=1)) in nights or d in self._mins():
                return False                                            # o dia do pernoite pedido (ou com mínimo de pernas) é de voo
            return super().inactive_today(d, loc, base, had_legs)

        def plan_day(self, d, loc, t_avail, base, final_day, must_end=None, end_limit=None, **kw):
            need = self._mins().get(d)
            self._cur_min = need
            if need:
                kw["max_legs"] = max(int(kw.get("max_legs") or self.p["legs_max"]), need)   # o mínimo pedido manda no teto do dia
                if kw.get("max_legs") > 4:
                    kw["wide"] = True
            want = self._nights().get(d)
            if want and not must_end and d not in getattr(self, "_night_conflict", {}):
                day = super().plan_day(d, loc, t_avail, base, final_day, want, end_limit, **kw)
                if day:
                    return day
                # nenhuma sequência termina no aeroporto pedido: o dia segue normal (o pedido sai como não atendido)
            return super().plan_day(d, loc, t_avail, base, final_day, must_end, end_limit, **kw)

        def plan(self, loc, t_avail, d_from, d_to, base, had_legs, must_end=None, end_limit=None):
            nights, mins = self._nights(), self._mins()
            if not nights and not mins:
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
                lt = to_local(o["dep"], self.tz(o["adep"]))
                view.append({"ld": lt.date(), "hour": lt.hour, "dep": o["dep"], "arr": o["arr"], "adep": o["adep"],
                             "ades": o["ades"], "ident": o.get("ident")})
            for d, want in sorted(nights.items()):
                at, last = sleep_at(view, d, loc)
                ok = bool(at) and crew_profiles.same_city(self.p["airline"], at, want)
                if ok:
                    arr = to_local(last["arr"], self.tz(last["ades"])) if last else None
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
    if not params.get("overnights") and not params.get("follow") and not params.get("day_legs_min") and not cargo.applies(params.get("airline"), params.get("include_cargo")):
        yield
        return
    with _PATCH_LOCK:
        orig = scheduler.Planner
        scheduler.Planner = _make_planner_class(orig)
        try:
            yield
        finally:
            scheduler.Planner = orig
