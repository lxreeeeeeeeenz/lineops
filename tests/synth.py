"""Gerador de telemetria SINTÉTICA para testes (nunca usado em produção)."""
from datetime import datetime, timedelta, timezone


def flight(start: datetime, a=(-15.8692, -47.9208), b=(-27.6705, -48.5453), cruise_ft=35000, fuel0=6500.0, with_bounce=False,
           touchdown_vs=-180, skip_out=False):
    """Gera amostras a 1 Hz-ish (passos maiores em cruzeiro). Devolve lista de dicts de telemetria."""
    out, t = [], start
    fuel = fuel0

    def s(lat, lon, alt, agl, gs, vs, ground, brake, eng=2, g=1.0):
        nonlocal t, fuel
        out.append(dict(t=t, lat=lat, lon=lon, alt_ft=alt, agl_ft=agl, gs_kt=gs, ias_kt=gs, vs_fpm=vs, hdg=180, on_ground=ground,
                        parking_brake=brake, engines=eng, fuel_kg=round(fuel, 1), gw_kg=60000 + fuel, g=g))

    def adv(sec):
        nonlocal t, fuel
        t += timedelta(seconds=sec)
        fuel -= sec * 0.7

    def lerp(f):
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f

    for _ in range(5):
        s(*a, 3500, 0, 0, 0, True, True); adv(2)           # no portão com freio
    if not skip_out:
        for _ in range(5):
            s(*a, 3500, 0, 8, 0, True, False); adv(10)     # táxi
    for gs in (40, 80, 120, 150):
        s(*a, 3500, 0, gs, 0, True, False); adv(5)         # corrida
    for i in range(6):
        s(*lerp(0.01 * (i + 1)), 3500 + 800 * i, 200 * i, 160, 1800, False, False); adv(10)   # subida
    for i in range(1, 60):
        f = 0.05 + 0.88 * i / 60
        s(*lerp(f), cruise_ft, cruise_ft - 3500, 440, 0, False, False); adv(120)   # cruzeiro
    for i in range(8):
        s(*lerp(0.94 + 0.005 * i), cruise_ft - 3000 * i, 20000 - 2500 * i, 300, -1800, False, False); adv(30)  # descida
    for i in range(6):
        s(*lerp(0.985 + 0.002 * i), 2500 - 400 * i, 2000 - 400 * i, 150, -700, False, False); adv(8)   # final
    s(*b, 70, 0, 135, touchdown_vs, True, False, g=1.25); adv(1)    # toque
    if with_bounce:
        s(*b, 80, 12, 130, 300, False, False); adv(2)
        s(*b, 70, 0, 130, -120, True, False, g=1.15); adv(1)
    for gs in (110, 80, 50, 30):
        s(*b, 70, 0, gs, 0, True, False); adv(5)
    for _ in range(4):
        s(*b, 70, 0, 12, 0, True, False); adv(10)
    for _ in range(4):
        s(*b, 70, 0, 0, 0, True, True); adv(3)
    return out
