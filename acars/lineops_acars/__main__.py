"""python -m lineops_acars --server http://PC:8787 --token gol_... --sim xplane [--flight ID]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, sims
from .client import Api, ApiError, Runner


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lineops_acars", description="LineOps ACARS (simulação; sem afiliação com a GOL).")
    ap.add_argument("--server", default=os.environ.get("LINEOPS_SERVER", "http://127.0.0.1:8787"))
    ap.add_argument("--token", default=os.environ.get("LINEOPS_TOKEN"), help="token de ACARS gerado na tela Configurações do LineOps")
    ap.add_argument("--sim", choices=["xplane", "msfs", "p3d", "replay"], default="xplane")
    ap.add_argument("--flight", type=int, help="ID do voo (se omitido, escolhe da lista)")
    ap.add_argument("--xp-host", default="127.0.0.1")
    ap.add_argument("--xp-port", type=int, default=49000)
    ap.add_argument("--simconnect-dll")
    ap.add_argument("--replay", help="arquivo .jsonl de telemetria (com --sim replay)")
    ap.add_argument("--confirm-restrictions", action="store_true", help="confirma restrições da aeronave (discrepâncias CONFIRMA)")
    ap.add_argument("--state-dir", default=str(Path.home() / ".lineops_acars"))
    ap.add_argument("--abort", action="store_true", help="aborta a sessão em andamento do voo e sai")
    ap.add_argument("--list", action="store_true", help="lista os voos disponíveis e sai")
    ap.add_argument("--version", action="version", version=__version__)
    if argv is None and len(sys.argv) == 1 and not os.environ.get("LINEOPS_TOKEN"):   # dois cliques no .exe: modo guiado
        from .interactive import run
        return run(main)
    a = ap.parse_args(argv)
    if not a.token:
        sys.exit("Informe --token (ou $LINEOPS_TOKEN). Gere um em LineOps → Configurações → Tokens do ACARS.")
    api = Api(a.server, a.token)
    try:
        flights = api.call("GET", "/api/acars/flights")
        if a.list or (not a.flight and not flights):
            for f in flights:
                print(f"{f['id']:>5}  {f['state']:<9} {f.get('flight_number') or '—':<9} {f['adep']}→{f['ades']}  {f.get('registration')}  {f.get('dep_utc') or ''}")
            if not flights:
                print("Nenhum voo reservado com aeronave. Reserve um voo da escala e atribua uma aeronave no LineOps.")
            return
        fid = a.flight
        if not fid:
            for f in flights:
                print(f"{f['id']:>5}  {f['state']:<9} {f.get('flight_number') or '—':<9} {f['adep']}→{f['ades']}  {f.get('registration')}  {f.get('dep_utc') or ''}")
            fid = int(input("ID do voo: ").strip())
        if a.abort:
            live = [s for s in api.call("GET", "/api/acars/live") if s["flight_id"] == fid]
            if not live:
                sys.exit("Nenhuma sessão ativa para este voo.")
            api.call("POST", f"/api/acars/sessions/{live[0]['id']}/abort", {"reason": "abortado pelo cliente"})
            print("Sessão abortada; voo voltou para RESERVADO.")
            return
        conn = sims.make(a.sim, xp_host=a.xp_host, xp_port=a.xp_port, dll=a.simconnect_dll, replay=a.replay)
        r = Runner(api, conn, fid, sim_label=sims.SIMS[a.sim], state_dir=Path(a.state_dir), confirm_restrictions=a.confirm_restrictions)
        r.start()
        print(f"Lendo {conn.name}… (Ctrl+C sai e mantém a sessão aberta para retomar)")
        r.run()
    except ApiError as e:
        sys.exit(f"Servidor recusou: {e}")
    except KeyboardInterrupt:
        print("\nSaindo; a sessão continua aberta no servidor (rode novamente para retomar, ou use --abort).")
    except (ConnectionError, OSError) as e:
        sys.exit(f"Não foi possível falar com o servidor em {a.server} ({e}). O LineOps está aberto (iniciar.bat)? O endereço e o IP estão certos?")


if __name__ == "__main__":
    main()
