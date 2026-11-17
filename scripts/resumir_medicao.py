"""
Resume as execuções de medição do custo da observabilidade.

Para cada execução (A, B, C, A2) lê:
  - o arquivo binário do sar  (ex.: runA.sa)     -> CPU, memória e rede do nó
  - o CSV do Locust           (ex.: overhead_A_stats.csv) -> latência, throughput, falhas

e imprime uma tabela com média e pico de CPU e memória, tráfego de saída da
interface física, percentis de latência, requisições por segundo e falhas,
além do acréscimo das execuções B e C sobre a A (sem observabilidade).

Uso (na pasta onde estão os arquivos):
    python3 resumir_medicao.py                      # procura A, B, C e A2
    python3 resumir_medicao.py --iface enp5s0 --skip 2

Requer o pacote sysstat (comando sadf) instalado.
"""

import argparse
import csv
import os
import subprocess
from datetime import datetime

RUNS = ["A", "B", "C", "A2", "B2", "C2"]
ROTULOS = {
    "A": "A (sem observabilidade)",
    "B": "B (padrão)",
    "C": "C (otimizada)",
    "A2": "A2 (sem observabilidade, repetição)",
    "B2": "B2 (padrão, repetição)",
    "C2": "C2 (otimizada, repetição)",
}


def sadf_texto(arquivo_sa):
    """Converte o binário do sar em texto CSV (separador ';')."""
    env = dict(os.environ, LC_ALL="C")
    out = subprocess.run(
        ["sadf", "-d", arquivo_sa, "--", "-u", "-r", "-n", "DEV"],
        capture_output=True, text=True, env=env, check=True,
    )
    return out.stdout


def _ts(txt):
    return datetime.strptime(txt[:19], "%Y-%m-%d %H:%M:%S")


def parse_sadf(texto):
    """Devolve amostras de CPU, memória e rede, cada uma com seu instante."""
    cpu, mem, rede = [], [], []
    colunas = None
    for linha in texto.splitlines():
        if not linha.strip():
            continue
        if linha.startswith("#"):
            colunas = linha.lstrip("# ").split(";")
            continue
        if colunas is None:
            continue
        campos = linha.split(";")
        if len(campos) != len(colunas):
            continue
        d = dict(zip(colunas, campos))
        t = _ts(d["timestamp"])
        if "%idle" in d and d.get("CPU", "-1") in ("-1", "all"):
            cpu.append((t, 100.0 - float(d["%idle"])))
        elif "kbmemused" in d:
            mem.append((t, float(d["kbmemused"]) / 1048576.0))  # GiB
        elif "IFACE" in d:
            rede.append((t, d["IFACE"], float(d["txkB/s"]), float(d["interval"])))
    return cpu, mem, rede


def separar_sessoes(cpu, intervalo_max=30):
    """Divide as amostras em sessões quando há um intervalo maior que intervalo_max segundos."""
    sessoes, inicio, anterior = [], None, None
    for t, _ in cpu:
        if inicio is None:
            inicio = t
        elif (t - anterior).total_seconds() > intervalo_max:
            sessoes.append((inicio, anterior))
            inicio = t
        anterior = t
    if inicio is not None:
        sessoes.append((inicio, anterior))
    return sessoes


def resumo_sar(arquivo_sa, iface, skip, sessao=None, rotulo=""):
    cpu, mem, rede = parse_sadf(sadf_texto(arquivo_sa))
    sessoes = separar_sessoes(cpu)
    if len(sessoes) > 1 and sessao is None:
        lista = "; ".join(f"[{i}] {a:%H:%M:%S} a {b:%H:%M:%S}" for i, (a, b) in enumerate(sessoes))
        raise SystemExit(f"{arquivo_sa} tem {len(sessoes)} sessões ({lista}). "
                         f"Indique qual usar com --sessao {rotulo}=N (use -1 para a última).")
    ini, fim = sessoes[0 if sessao is None else sessao]
    cpu_v = [v for t, v in cpu if ini <= t <= fim][skip:]
    mem_v = [v for t, v in mem if ini <= t <= fim][skip:]
    tx_mb = sum(tx * iv for t, i, tx, iv in rede if i == iface and ini <= t <= fim) / 1024.0
    return {
        "cpu_med": sum(cpu_v) / len(cpu_v), "cpu_pico": max(cpu_v),
        "mem_med": sum(mem_v) / len(mem_v), "mem_pico": max(mem_v),
        "tx_mb": tx_mb, "amostras": len(cpu_v),
    }


def resumo_locust(csv_stats):
    with open(csv_stats, newline="", encoding="utf-8") as f:
        for linha in csv.DictReader(f):
            if linha["Name"] == "Aggregated":
                return {
                    "reqs": int(linha["Request Count"]),
                    "falhas": int(linha["Failure Count"]),
                    "rps": float(linha["Requests/s"]),
                    "p50": float(linha["50%"]), "p95": float(linha["95%"]),
                    "p99": float(linha["99%"]), "p100": float(linha["100%"]),
                }
    raise ValueError(f"Linha 'Aggregated' não encontrada em {csv_stats}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="enp5s0", help="interface física para o tráfego de saída")
    ap.add_argument("--skip", type=int, default=0, help="amostras iniciais do sar a ignorar")
    ap.add_argument("--prefixo", default="overhead", help="prefixo dos CSVs do Locust")
    ap.add_argument("--sa", action="append", default=[], metavar="RUN=arquivo",
                    help="arquivo do sar de uma execução, se não for runX.sa (ex.: A2=runA.sa)")
    ap.add_argument("--sessao", action="append", default=[], metavar="RUN=N",
                    help="sessão a usar quando o arquivo tem mais de uma (0 = primeira, -1 = última)")
    args = ap.parse_args()
    arquivos = dict(x.split("=", 1) for x in args.sa)
    sessoes = {k: int(v) for k, v in (x.split("=", 1) for x in args.sessao)}

    dados = {}
    for r in RUNS:
        sa = arquivos.get(r, f"run{r}.sa")
        csv_stats = f"{args.prefixo}_{r}_stats.csv"
        if os.path.exists(sa) and os.path.exists(csv_stats):
            dados[r] = {**resumo_sar(sa, args.iface, args.skip, sessoes.get(r), r),
                        **resumo_locust(csv_stats)}
        else:
            print(f"(execução {r} ignorada: faltam {sa} ou {csv_stats})")

    if not dados:
        raise SystemExit("Nenhuma execução encontrada.")

    print("\n| Execução | CPU méd (%) | CPU pico (%) | Mem méd (GiB) | Mem pico (GiB) | Rede TX (MB) | P50 | P95 | P99 | req/s | Falhas |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r, d in dados.items():
        print(f"| {ROTULOS[r]} | {d['cpu_med']:.1f} | {d['cpu_pico']:.1f} | {d['mem_med']:.2f} | "
              f"{d['mem_pico']:.2f} | {d['tx_mb']:.1f} | {d['p50']:.0f} | {d['p95']:.0f} | "
              f"{d['p99']:.0f} | {d['rps']:.2f} | {d['falhas']} |")

    if "A" in dados:
        base_runs = [r for r in ("A", "A2") if r in dados]
        chaves = ("cpu_med", "mem_med", "p50", "p95", "p99", "rps", "tx_mb")
        base = {k: sum(dados[r][k] for r in base_runs) / len(base_runs) for k in chaves}
        nome_base = " e ".join(base_runs)
        print(f"\nAcréscimo sobre a linha de base (média de {nome_base}):")
        print("| Execução | CPU méd (p.p.) | Mem méd (GiB) | Rede TX (MB) | P50 (ms) | P95 (ms) | P99 (ms) | req/s |")
        print("|---|---|---|---|---|---|---|---|")
        for r in ("B", "C", "B2", "C2"):
            if r in dados:
                d = dados[r]
                print(f"| {ROTULOS[r]} | {d['cpu_med']-base['cpu_med']:+.1f} | {d['mem_med']-base['mem_med']:+.2f} | "
                      f"{d['tx_mb']-base['tx_mb']:+.1f} | {d['p50']-base['p50']:+.0f} | {d['p95']-base['p95']:+.0f} | "
                      f"{d['p99']-base['p99']:+.0f} | {d['rps']-base['rps']:+.2f} |")

    if "A" in dados and "A2" in dados:
        a, a2 = dados["A"], dados["A2"]
        print("\nVariação entre as duas execuções sem observabilidade (A2 menos A), "
              "que indica o ruído do ambiente:")
        print(f"  CPU méd {a2['cpu_med']-a['cpu_med']:+.1f} p.p. | Mem méd {a2['mem_med']-a['mem_med']:+.2f} GiB | "
              f"P50 {a2['p50']-a['p50']:+.0f} ms | P95 {a2['p95']-a['p95']:+.0f} ms | "
              f"req/s {a2['rps']-a['rps']:+.2f}")


if __name__ == "__main__":
    main()
