"""
Detecção por métricas e tempo até o primeiro sinal da falha, nas execuções JÁ FEITAS
(sem nova coleta). Lê a janela de falha de cada execução em medicao_log.csv e consulta:

  PROMETHEUS  CPU e memória do nó (e consultas extras de --extra): linha de base antes da
              falha (média e desvio-padrão), pico durante a falha e primeiro instante em que a
              série fica acima de baseline + K desvios por dois pontos seguidos.
  LOKI        primeira linha de erro e primeira mensagem do Keystone depois do início da falha,
              mais a taxa de erro de fundo antes da falha (se for > 0, "primeiro erro" não
              distingue a falha do ruído; use a mensagem do Keystone).
  JAEGER      (opcional, --jaeger) traces curtos (<= 2 spans) com duração perto de 3 s na janela
              da falha e a fração deles com mensagem do Keystone no Loki a +-5 s do fim do trace.
              Isso VERIFICA a leitura "requisição de ~3 s = espera por autenticação".

IMPORTANTE: o tempo medido é até o PRIMEIRO SINAL VISÍVEL nos dados, e não até um alerta do
Alertmanager. As séries do Prometheus têm resolução do intervalo de coleta (15 ou 30 s) e a CPU
usa rate de 2 min, então o atraso tem resolução de cerca de 2 min.
O Prometheus guarda 15 dias e o Jaeger 72 h: rode logo.

Uso (de qualquer máquina que alcance a AWS; precisa estar na mesma pasta do coletar_qa2.py):
  python3 analisar_deteccao.py --log medicao_log.csv --prefixo n2_ n3_ --conds BC \
      --prom http://IP_DO_SERVIDOR:9090 --loki http://IP_DO_SERVIDOR:3100 \
      --jaeger http://IP_DO_SERVIDOR:16686 --host caio-cezar | tee deteccao.txt
"""
import argparse
import csv
import math
import statistics as st

import requests

from coletar_qa2 import PADROES, jaeger_traces, loki_contagem


def prom_serie(url, q, ini, fim, step=15):
    r = requests.get(f"{url}/api/v1/query_range",
                     params={"query": q, "start": ini, "end": fim, "step": step}, timeout=60)
    r.raise_for_status()
    res = r.json()["data"]["result"]
    if not res:
        return []
    return [(float(t), float(v)) for t, v in res[0]["values"] if v not in ("NaN", "+Inf", "-Inf")]


def analisa_serie(pts, fi, ff, k):
    base = [v for t, v in pts if fi - 180 <= t < fi - 10]
    dur = [(t, v) for t, v in pts if fi <= t <= ff]
    if len(base) < 3 or not dur:
        return None
    m, s = st.mean(base), st.stdev(base)
    s = max(s, 0.01 * abs(m), 1e-9)
    lim = m + k * s
    pico = max(v for _, v in dur)
    primeiro = None
    for i in range(len(dur) - 1):
        if dur[i][1] > lim and dur[i + 1][1] > lim:
            primeiro = dur[i][0] - fi
            break
    return {"base": m, "dp": s, "pico": pico, "lat": primeiro}


def loki_primeiro(url, seletor, filtro, ini, fim):
    r = requests.get(f"{url}/loki/api/v1/query_range", params={
        "query": f"{seletor} {filtro}", "start": int(ini * 1e9), "end": int(fim * 1e9),
        "limit": 1, "direction": "forward"}, timeout=60)
    r.raise_for_status()
    ts = [int(v[0]) / 1e9 for s in r.json()["data"]["result"] for v in s["values"]]
    return min(ts) if ts else None


def loki_tempos(url, seletor, filtro, ini, fim, limite=5000):
    r = requests.get(f"{url}/loki/api/v1/query_range", params={
        "query": f"{seletor} {filtro}", "start": int(ini * 1e9), "end": int(fim * 1e9),
        "limit": limite, "direction": "forward"}, timeout=120)
    r.raise_for_status()
    return sorted(int(v[0]) / 1e9 for s in r.json()["data"]["result"] for v in s["values"])


def ms(v):
    v = [x for x in v if x is not None]
    if not v:
        return "sem sinal"
    return f"{st.mean(v):.0f} ± {st.stdev(v) if len(v) > 1 else 0:.0f}"


def fmt(x, nd=1):
    return "sem dados" if x is None else f"{x:.{nd}f}".replace(".", ",")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="medicao_log.csv")
    ap.add_argument("--prefixo", nargs="+", required=True, help="ex.: n2_ n3_")
    ap.add_argument("--conds", default="BC")
    ap.add_argument("--blocos", type=int, default=5)
    ap.add_argument("--prom", required=True)
    ap.add_argument("--loki", default=None)
    ap.add_argument("--jaeger", default=None)
    ap.add_argument("--host", default="caio-cezar", help="rótulo host_name das métricas do nó")
    ap.add_argument("--seletor", default='{service_name=~".+"}')
    ap.add_argument("--k", type=float, default=3.0, help="desvios acima da linha de base")
    ap.add_argument("--margem", type=int, default=15)
    ap.add_argument("--extra", action="append", default=[], help='"nome=consulta PromQL" (repetível)')
    ap.add_argument("--curto-min", type=float, default=2500.0)
    ap.add_argument("--curto-max", type=float, default=3500.0)
    a = ap.parse_args()

    H = a.host
    metricas = {
        "CPU do nó (%)": f'100 * (1 - (sum(rate(system_cpu_time_seconds_total{{state="idle", host_name="{H}"}}[2m])) / sum(rate(system_cpu_time_seconds_total{{host_name="{H}"}}[2m]))))',
        "Memória do nó (GiB)": f'system_memory_usage_bytes{{state="used", host_name="{H}"}} / 1024^3',
    }
    for e in a.extra:
        n, _, q = e.partition("=")
        metricas[n.strip()] = q.strip()

    log = {}
    with open(a.log, newline="") as f:
        for lin in csv.reader(f):
            if len(lin) < 8 or not lin[3].strip().isdigit():
                continue
            log[lin[0]] = {"ini": int(lin[3]), "fim": int(lin[4]), "falha": lin[5],
                           "fi": int(lin[6] or 0), "ff": int(lin[7] or 0)}

    linhas, agreg = [], {}
    for pref in a.prefixo:
        for c in a.conds:
            for b in range(1, a.blocos + 1):
                n = f"{pref}{c}{b}"
                r = log.get(n)
                if not r:
                    print(f"(execução {n} ausente em {a.log})")
                    continue
                if r["fi"] == 0 or r["falha"] in ("none", ""):
                    continue
                fi, ff = r["fi"], min(r["ff"] + a.margem, r["fim"])
                d = {"nome": n, "cond": c, "falha": r["falha"], "pref": pref}
                for nome, q in metricas.items():
                    try:
                        d[nome] = analisa_serie(prom_serie(a.prom, q, fi - 300, ff + 120), fi, ff, a.k)
                    except Exception as e:
                        print(f"[{n}] erro no Prometheus ({nome}): {e}")
                        d[nome] = None
                if a.loki:
                    try:
                        t1 = loki_primeiro(a.loki, a.seletor, PADROES["erro"], fi, ff)
                        t2 = loki_primeiro(a.loki, a.seletor, PADROES["keystone"], fi, ff)
                        d["lat_erro"] = None if t1 is None else t1 - fi
                        d["lat_keystone"] = None if t2 is None else t2 - fi
                        d["bg_erro"] = loki_contagem(a.loki, a.seletor, PADROES["erro"], r["ini"], fi) / max((fi - r["ini"]) / 60, 1e-9)
                    except Exception as e:
                        print(f"[{n}] erro no Loki: {e}")
                if a.jaeger and a.loki:
                    try:
                        tr, _ = jaeger_traces(a.jaeger, fi, ff, 5000, {"jaeger-all-in-one", "jaeger-query"})
                        curtos = [t for t in tr if t["n_spans"] <= 2 and a.curto_min <= t["dur_ms"] <= a.curto_max]
                        ks = loki_tempos(a.loki, a.seletor, PADROES["keystone"], fi - 30, ff + 30)
                        casados = sum(1 for t in curtos
                                      if any(abs(x - (t["ini"] + t["dur_ms"] / 1000)) <= 5 for x in ks))
                        d["curtos"] = len(curtos)
                        d["casados"] = casados
                    except Exception as e:
                        print(f"[{n}] erro no Jaeger: {e}")
                linhas.append(d)
                agreg.setdefault((pref, c), []).append(d)

    print("\n## Por execução\n")
    cab = ["Execução", "Falha"] + [f"{m}: base / pico / 1º sinal (s)" for m in metricas]
    if a.loki:
        cab += ["1º erro (s)", "1º aviso Keystone (s)", "erro de fundo (linhas/min)"]
    if a.jaeger and a.loki:
        cab += ["traces curtos ~3 s", "com aviso Keystone ±5 s"]
    print("| " + " | ".join(cab) + " |")
    print("|" + "---|" * len(cab))
    for d in linhas:
        cel = [d["nome"], d["falha"]]
        for m in metricas:
            x = d.get(m)
            cel.append("sem dados" if x is None else
                       f"{fmt(x['base'])} / {fmt(x['pico'])} / " + ("sem sinal" if x["lat"] is None else f"{x['lat']:.0f}"))
        if a.loki:
            cel += [fmt(d.get("lat_erro"), 0), fmt(d.get("lat_keystone"), 0), fmt(d.get("bg_erro"))]
        if a.jaeger and a.loki:
            cel += [str(d.get("curtos", "-")), str(d.get("casados", "-"))]
        print("| " + " | ".join(cel) + " |")

    print("\n## Por condição: média ± desvio-padrão do tempo até o 1º sinal (s), e detecções/execuções\n")
    cab2 = ["Cenário (prefixo)", "Condição"] + list(metricas)
    if a.loki:
        cab2 += ["1º aviso Keystone"]
    print("| " + " | ".join(cab2) + " |")
    print("|" + "---|" * len(cab2))
    for (pref, c), ds in agreg.items():
        cel = [pref, c]
        for m in metricas:
            lat = [x[m]["lat"] for x in ds if x.get(m)]
            det = sum(1 for v in lat if v is not None)
            cel.append(f"{ms(lat)} ({det}/{len(ds)})")
        if a.loki:
            lat = [x.get("lat_keystone") for x in ds]
            cel.append(f"{ms(lat)} ({sum(1 for v in lat if v is not None)}/{len(ds)})")
        print("| " + " | ".join(cel) + " |")
    if a.jaeger and a.loki:
        tot = sum(d.get("curtos", 0) for d in linhas)
        cas = sum(d.get("casados", 0) for d in linhas)
        if tot:
            print(f"\nTraces curtos de ~3 s na janela da falha: {tot}; com aviso do Keystone no Loki a ±5 s: {cas} ({100*cas/tot:.0f}%).")
    print("\nLeitura: 'sem sinal' = a série não passou de baseline + "
          f"{a.k:g} desvios por dois pontos seguidos; não prova ausência de efeito. O tempo é até o primeiro sinal "
          "visível nos dados, não até um alerta.")


if __name__ == "__main__":
    main()
