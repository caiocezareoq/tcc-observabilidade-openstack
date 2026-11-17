"""
Lê no Prometheus as métricas internas dos processadores de amostragem do gateway
(tail_sampling e probabilistic_sampler), na janela de uma execução (job
otel-collector-internal, porta 8888), para descobrir QUAL amostragem estava ativa.

Uso:  python3 consultar_tail_sampling.py --prom http://IP_DO_SERVIDOR:9090 --log medicao_log.csv c2_C1 c2_C2
Mostra o aumento (increase) de cada série não nula durante a execução + 60 s.
Procure por: count_traces_sampled (por policy e sampled), sampling_trace_dropped_too_early,
sampling_late_span_age (spans que chegaram depois da decisão), new_trace_id_received.
"""
import argparse
import csv
import sys

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--prom", required=True)
ap.add_argument("--log", default="medicao_log.csv")
ap.add_argument("nomes", nargs="+")
a = ap.parse_args()

log = {}
with open(a.log, newline="") as f:
    for lin in csv.reader(f):
        if len(lin) >= 5 and lin[3].strip().isdigit():
            log[lin[0]] = (int(lin[3]), int(lin[4]))

for n in a.nomes:
    if n not in log:
        print(f"{n}: ausente em {a.log}")
        continue
    ini, fim = log[n]
    sel = '{__name__=~"otelcol_processor_(tail_sampling|probabilistic_sampler).*",__name__!~".*_bucket"}'

    def inst(t):
        # valor bruto dos contadores no instante t (sem increase(), que dá erro 422
        # quando séries de nomes diferentes ficam com os mesmos rótulos)
        r = requests.get(f"{a.prom}/api/v1/query", params={"query": sel, "time": t}, timeout=60)
        r.raise_for_status()
        return {tuple(sorted(x["metric"].items())): float(x["value"][1])
                for x in r.json()["data"]["result"]}

    # Quais séries de processadores existiam na janela (prova de que o gateway foi coletado)
    rs = requests.get(f"{a.prom}/api/v1/series", params={
        "match[]": '{__name__=~"otelcol_processor_.*",__name__!~".*_bucket"}',
        "start": ini, "end": fim + 60}, timeout=60)
    rs.raise_for_status()
    nomes = sorted({x["__name__"].replace("otelcol_processor_", "") for x in rs.json()["data"]})
    print(f"\n[{n}] métricas de processadores presentes na janela: "
          f"{', '.join(nomes) if nomes else 'NENHUMA (o gateway não foi coletado nesse período)'}")
    antes, depois = inst(ini - 1), inst(fim + 60)
    print(f"\n== {n} (janela {fim - ini + 61}s) ==")
    if not depois:
        print("  nenhuma série no fim da janela: o processador não emitiu métricas nesse período.")
    for chave, vf in sorted(depois.items()):
        vi = antes.get(chave, 0.0)
        v = vf - vi if vf >= vi else vf  # contador reiniciado (contêiner recriado)
        if v == 0:
            continue
        m = dict(chave)
        rot = ",".join(f"{k}={m[k]}" for k in ("processor", "policy", "sampled", "otel_signal") if k in m)
        print(f"  {m.get('__name__','?').replace('otelcol_processor_','')}"
              f"{{{rot}}} = {v:.0f}")
