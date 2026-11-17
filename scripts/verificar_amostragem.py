"""
Descobre qual amostragem de traces o gateway tinha em cada execução, usando só o que o
Prometheus guardou. Para cada execução do log, consulta (uma métrica por vez, para evitar
conflito de rótulos) quantas amostras de cada métrica existem na janela e, se existirem, o
aumento do contador.

  controle   otelcol_processor_batch_timeout_trigger_send   -> o gateway foi coletado?
  tail       otelcol_processor_tail_sampling_new_trace_id_received
  prob       otelcol_processor_probabilistic_sampler_count_traces_sampled

Uso:  python3 verificar_amostragem.py --prom http://IP_DO_SERVIDOR:9090 --log medicao_log.csv \
          c2_B1 c2_C1 c2_C3 teste_T1
Leitura: controle > 0 e tail = 0 e prob = 0  -> o gateway não tinha nenhum dos dois processadores.
         tail > 0 -> tail sampling ativo.   prob > 0 -> amostragem probabilística ativa.
"""
import argparse
import csv

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--prom", required=True)
ap.add_argument("--log", default="medicao_log.csv")
ap.add_argument("--esperado", choices=["B", "C", "P"], default=None,
                help="condição esperada: B (sem amostragem), C (tail sampling) ou P (probabilístico). "
                     "Se informada, imprime OK ou ERRO e sai com código 1 em caso de erro.")
ap.add_argument("nomes", nargs="+")
a = ap.parse_args()

METRICAS = [
    ("controle (batch)", "otelcol_processor_batch_timeout_trigger_send"),
    ("tail_sampling", "otelcol_processor_tail_sampling_new_trace_id_received"),
    ("probabilistic", "otelcol_processor_probabilistic_sampler_count_traces_sampled"),
]

log = {}
with open(a.log, newline="") as f:
    for lin in csv.reader(f):
        if len(lin) >= 5 and lin[3].strip().isdigit():
            log[lin[0]] = (int(lin[3]), int(lin[4]))


def q(expr, t):
    r = requests.get(f"{a.prom}/api/v1/query", params={"query": expr, "time": t}, timeout=60)
    r.raise_for_status()
    return r.json()["data"]["result"]


erros = 0
for n in a.nomes:
    if n not in log:
        print(f"{n}: ausente em {a.log}")
        continue
    cont = {}
    ini, fim = log[n]
    w = fim - ini
    print(f"\n== {n} (janela {w}s, {ini} a {fim}) ==")
    for rot, nome in METRICAS:
        amostras = q(f"count_over_time({nome}[{w}s])", fim)
        n_am = max((float(x["value"][1]) for x in amostras), default=0)
        cresc = q(f"sum(increase({nome}[{w}s]))", fim)
        v = float(cresc[0]["value"][1]) if cresc else float("nan")
        cont[nome] = n_am
        print(f"  {rot:18s} amostras na janela: {n_am:4.0f}   aumento do contador: {v:.0f}")
    if a.esperado:
        ctrl, tail, prob = (cont[m[1]] for m in METRICAS)
        ok = ctrl > 0 and {"B": tail == 0 and prob == 0,
                           "C": tail > 0 and prob == 0,
                           "P": prob > 0 and tail == 0}[a.esperado]
        if ok:
            print(f"  >>> OK: gateway coerente com a condição {a.esperado}")
        else:
            erros += 1
            motivo = ("gateway não coletado pelo Prometheus" if ctrl == 0 else
                      f"tail={tail:.0f}, prob={prob:.0f} amostras")
            print(f"  >>> ERRO: gateway NÃO está na configuração da condição {a.esperado} ({motivo}). "
                  f"Descarte {n} e corrija o gateway antes de continuar.")

raise SystemExit(1 if erros else 0)
