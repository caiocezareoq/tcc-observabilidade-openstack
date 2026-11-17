"""
Resume a rodada com repetição (condições A, B e C, N blocos).

Espera, para cada execução NOME (A1..A5, B1..B5, C1..C5):
  runNOME.sa, overhead_NOME_stats.csv   (e medicao_log.csv, gerado por rodar_medicao.sh)

Imprime:
  1. tabela por condição: média ± desvio-padrão (n execuções) de CPU, memória,
     tráfego de saída, P50/P95/P99, req/s e falhas;
  2. diferenças pareadas por bloco (B-A, C-A, C-B): média e intervalo de confiança
     de 95% (t de Student). Se o intervalo não contém zero, a diferença é
     distinguível do ruído entre execuções;
  3. (opcional, com --prom) CPU, memória e tráfego de entrada do Servidor de
     Observabilidade na janela de cada execução B e C, consultados no Prometheus.

Uso:
    python3 resumir_rodada.py --iface enp5s0 --skip 2
    python3 resumir_rodada.py --iface enp5s0 --skip 2 --prom http://IP-AWS:9090
Requer resumir_medicao.py na mesma pasta e o pacote sysstat (sadf).
"""
import argparse
import csv
import math
import os
import statistics as st

from resumir_medicao import resumo_locust, resumo_sar

# t de Student bicaudal, 95%, por graus de liberdade (n-1)
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228}
COND = {"A": "A (sem observabilidade)", "B": "B (padrão)", "C": "C (otimizada)"}
CAMPOS = [("cpu_med", "CPU méd (%)", 1), ("mem_med", "Mem méd (GiB)", 2),
          ("tx_mb", "Rede TX (MB)", 1), ("p50", "P50 (ms)", 0), ("p95", "P95 (ms)", 0),
          ("p99", "P99 (ms)", 0), ("rps", "req/s", 2), ("falhas", "Falhas", 0)]


def media_dp(v):
    return st.mean(v), (st.stdev(v) if len(v) > 1 else 0.0)


def ic95(dif):
    n = len(dif)
    if n < 2:
        return st.mean(dif), float("nan")
    return st.mean(dif), T95.get(n - 1, 1.96) * st.stdev(dif) / math.sqrt(n)


def prom_servidor(url, ini, fim):
    """CPU, memória e tráfego de entrada do servidor na janela [ini, fim].

    CPU e rede usam increase() sobre a janela inteira (uma consulta instantânea em
    'fim'), o que não depende do intervalo de coleta (15 s no padrão, 30 s na
    otimizada). Com rate() em janela curta, a configuração de 30 s fica sem
    amostras suficientes e a consulta volta vazia.
    """
    import requests

    def inst(expr):
        r = requests.get(f"{url}/api/v1/query", params={"query": expr, "time": fim}, timeout=30)
        r.raise_for_status()
        res = r.json()["data"]["result"]
        return float(res[0]["value"][1]) if res else float("nan")

    def faixa(expr):
        r = requests.get(f"{url}/api/v1/query_range", params={
            "query": expr, "start": ini, "end": fim, "step": "5s"}, timeout=30)
        r.raise_for_status()
        res = r.json()["data"]["result"]
        return [float(x[1]) for x in res[0]["values"]] if res else []

    w = f"{int(fim - ini)}s"
    job = 'job="otel-collector-host-aws"'
    cpu = inst(f'100 * (1 - sum(increase(system_cpu_time_seconds_total{{state="idle",{job}}}[{w}])) / '
               f'sum(increase(system_cpu_time_seconds_total{{{job}}}[{w}])))')
    rx = inst(f'sum(increase(system_network_io_bytes_total{{direction="receive",{job},'
              f'device!~"lo|docker.*|veth.*|br-.*"}}[{w}]))')
    mem = faixa(f'system_memory_usage_bytes{{state="used",{job}}}')
    return {"srv_cpu": cpu,
            "srv_mem": st.mean(mem) / 2**30 if mem else float("nan"),
            "srv_rx_mb": rx / 1e6}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="enp5s0")
    ap.add_argument("--skip", type=int, default=2, help="amostras iniciais do sar a ignorar")
    ap.add_argument("--blocos", type=int, default=5)
    ap.add_argument("--prom", default=None, help="URL do Prometheus da AWS (opcional)")
    ap.add_argument("--conds", default="ABC", help="condições presentes (ex.: BC nos Cenários 2 e 3)")
    ap.add_argument("--prefixo", default="", help="prefixo dos nomes das execuções (ex.: c2_)")
    ap.add_argument("--log", default="medicao_log.csv", help="arquivo de registro das execuções")
    a = ap.parse_args()

    dados = {}
    for c in a.conds:
        for b in range(1, a.blocos + 1):
            n = f"{a.prefixo}{c}{b}"
            sa, cs = f"run{n}.sa", f"overhead_{n}_stats.csv"
            if os.path.exists(sa) and os.path.exists(cs):
                dados[n] = {**resumo_sar(sa, a.iface, a.skip, rotulo=n), **resumo_locust(cs)}
            else:
                print(f"(execução {n} ausente: faltam {sa} ou {cs})")

    print("\n## Por condição: média ± desvio-padrão (n execuções)\n")
    print("| Condição | n | " + " | ".join(r for _, r, _ in CAMPOS) + " |")
    print("|---|---|" + "---|" * len(CAMPOS))
    for c in a.conds:
        runs = [dados[f"{a.prefixo}{c}{b}"] for b in range(1, a.blocos + 1) if f"{a.prefixo}{c}{b}" in dados]
        if not runs:
            continue
        cel = []
        for k, _, d in CAMPOS:
            m, s = media_dp([r[k] for r in runs])
            cel.append(f"{m:.{d}f} ± {s:.{d}f}")
        print(f"| {COND[c]} | {len(runs)} | " + " | ".join(cel) + " |")

    print("\n## Diferenças pareadas por bloco: média [IC 95%]\n")
    print("| Comparação | blocos | " + " | ".join(r for _, r, _ in CAMPOS[:-1]) + " |")
    print("|---|---|" + "---|" * (len(CAMPOS) - 1))
    for x, y in (("B", "A"), ("C", "A"), ("C", "B")):
        if x not in a.conds or y not in a.conds:
            continue
        blocos = [b for b in range(1, a.blocos + 1)
                  if f"{a.prefixo}{x}{b}" in dados and f"{a.prefixo}{y}{b}" in dados]
        if not blocos:
            continue
        cel = []
        for k, _, d in CAMPOS[:-1]:
            m, h = ic95([dados[f"{a.prefixo}{x}{b}"][k] - dados[f"{a.prefixo}{y}{b}"][k] for b in blocos])
            cel.append(f"{m:+.{d}f} [{m-h:+.{d}f}; {m+h:+.{d}f}]")
        print(f"| {x} − {y} | {len(blocos)} | " + " | ".join(cel) + " |")

    falhas = {n: d["falhas"] for n, d in dados.items() if d["falhas"]}
    print("\nExecuções com falhas de requisição:", falhas or "nenhuma")

    if a.prom:
        log = {}
        if os.path.exists(a.log):
            with open(a.log, newline="") as f:
                log = {r["nome"]: r for r in csv.DictReader(f)}
        print("\n## Servidor de Observabilidade (AWS), por execução\n")
        print("| Execução | CPU servidor (%) | Mem servidor (GiB) | Rede RX (MB) |")
        print("|---|---|---|---|")
        por_cond = {"B": [], "C": []}
        for c in "BC":
            for b in range(1, a.blocos + 1):
                r = log.get(f"{a.prefixo}{c}{b}")
                if not r:
                    print(f"| {a.prefixo}{c}{b} | (sem registro em {a.log}) | | |")
                    continue
                try:
                    s_ = prom_servidor(a.prom, int(r["inicio_epoch"]), int(r["fim_epoch"]))
                except Exception as e:  # consulta falhou
                    print(f"| {a.prefixo}{c}{b} | erro na consulta: {e} | | |")
                    continue
                por_cond[c].append(s_)
                print(f"| {a.prefixo}{c}{b} | {s_['srv_cpu']:.1f} | {s_['srv_mem']:.2f} | {s_['srv_rx_mb']:.1f} |")
        print("\n## Servidor de Observabilidade (AWS): média ± desvio-padrão (valores válidos)\n")
        print("| Condição | n | CPU servidor (%) | Mem servidor (GiB) | Rede RX (MB) |")
        print("|---|---|---|---|---|")
        for c in "BC":
            cel = []
            for k, d in (("srv_cpu", 1), ("srv_mem", 2), ("srv_rx_mb", 1)):
                v = [x[k] for x in por_cond[c] if not math.isnan(x[k])]
                if len(v) < len(por_cond[c]):
                    print(f"(aviso: {c}, {k}: {len(por_cond[c]) - len(v)} execução(ões) sem dados no Prometheus)")
                cel.append(f"{media_dp(v)[0]:.{d}f} ± {media_dp(v)[1]:.{d}f} (n={len(v)})" if v else "sem dados")
            print(f"| {COND[c]} | {len(por_cond[c])} | " + " | ".join(cel) + " |")


if __name__ == "__main__":
    main()
