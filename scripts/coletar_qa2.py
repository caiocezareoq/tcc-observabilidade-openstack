"""
Coleta as evidências da QA2 (detecção e diagnóstico) no Loki e no Jaeger, na janela
de cada execução registrada em medicao_log.csv.

Para cada execução, divide o tempo em três janelas usando os horários da falha que o
rodar_medicao.sh registrou: ANTES da falha, DURANTE (falha + margem de recuperação) e
DEPOIS. Mede:

  LOKI   linhas de log por minuto, em cada janela: total, com erro, com a mensagem de
         autenticação do Keystone e de avisos de depreciação (ruído de fundo).
  JAEGER traces criados na execução e na janela da falha: total, com erro, lentos
         (>= limiar, 500 ms por padrão), e a lista dos lentos com --detalhe.

Os traces são deduplicados por traceID entre serviços.

Uso (de qualquer máquina que alcance a AWS):
    python3 coletar_qa2.py --log medicao_log_c2.csv --prefixo c2_ --conds BC \
        --loki http://IP_DO_SERVIDOR:3100 --jaeger http://IP_DO_SERVIDOR:16686

    # comparação das amostragens (condições C e P, ver roteiro_qa2_amostragem.md):
    python3 coletar_qa2.py --log medicao_log.csv --prefixo amo_ --conds CP --detalhe \
        --loki ... --jaeger ...

ATENÇÃO: o Jaeger com Badger guarda os traces por 72 h por padrão
(BADGER_SPAN_STORE_TTL). Execuções mais antigas voltam com zero traces. O script avisa.
Os rótulos do Loki variam com a ingestão OTLP: o script lista os rótulos encontrados e
usa --seletor para o fluxo de logs (padrão {service_name=~".+"}).
"""
import argparse
import csv
import math
import re
import statistics as st
import sys

import requests

T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228}
NOMES = {"B": "B (padrão)", "C": "C (otimizada, tail sampling)",
         "P": "P (otimizada, amostragem probabilística)",
         "D": "D (otimizada, tail sampling, decision_wait 30 s)"}

PADROES = {
    "total": "",
    "erro": r'|~ "(?i)error|critical|traceback"',
    "keystone": r'|~ "(?i)auth_token|unable to validate token|service unavailable"',
    "deprec": r'|~ "(?i)deprecat"',
}


def media_dp(v):
    return (st.mean(v), st.stdev(v) if len(v) > 1 else 0.0) if v else (float("nan"), float("nan"))


def ic95(dif):
    n = len(dif)
    if n < 2:
        return (st.mean(dif) if dif else float("nan")), float("nan")
    return st.mean(dif), T95.get(n - 1, 1.96) * st.stdev(dif) / math.sqrt(n)


# ----------------------------------------------------------------------------- Loki
def loki_rotulos(url):
    try:
        r = requests.get(f"{url}/loki/api/v1/labels", timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        return [f"(falha ao listar rótulos: {e})"]


def loki_contagem(url, seletor, filtro, ini, fim):
    dur = max(int(fim - ini), 1)
    q = f"sum(count_over_time({seletor} {filtro} [{dur}s]))"
    r = requests.get(f"{url}/loki/api/v1/query", params={"query": q, "time": int(fim)}, timeout=60)
    r.raise_for_status()
    res = r.json()["data"]["result"]
    return float(res[0]["value"][1]) if res else 0.0


def loki_janelas(url, seletor, janelas):
    """janelas: {nome: (ini, fim)} -> {nome: {tipo: linhas/min}}"""
    out = {}
    for nome, (ini, fim) in janelas.items():
        if fim <= ini:
            out[nome] = {k: float("nan") for k in PADROES}
            continue
        minutos = (fim - ini) / 60
        out[nome] = {k: loki_contagem(url, seletor, f, ini, fim) / minutos for k, f in PADROES.items()}
    return out


# --------------------------------------------------------------------------- Jaeger
def jaeger_traces(url, ini, fim, limite, ignorar):
    """Todos os traces iniciados em [ini, fim], deduplicados. Retorna (lista, truncado)."""
    r = requests.get(f"{url}/api/services", timeout=30)
    r.raise_for_status()
    servicos = [s for s in (r.json().get("data") or []) if s not in ignorar]
    vistos, truncado = {}, False
    for s in servicos:
        r = requests.get(f"{url}/api/traces", params={
            "service": s, "start": int(ini * 1e6), "end": int(fim * 1e6), "limit": limite},
            timeout=120)
        r.raise_for_status()
        dados = r.json().get("data") or []
        if len(dados) >= limite:
            truncado = True
        for t in dados:
            vistos.setdefault(t["traceID"], t)
    return [resumir_trace(t) for t in vistos.values()], truncado


def _eh_erro(tags):
    for tg in tags:
        k, v = tg.get("key"), tg.get("value")
        if k == "error" and str(v).lower() == "true":
            return True
        if k == "otel.status_code" and str(v).upper() == "ERROR":
            return True
        if k in ("http.status_code", "http.response.status_code"):
            try:
                if int(v) >= 500:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def resumir_trace(t):
    spans = t.get("spans", [])
    procs = t.get("processes", {})
    ini = min(s["startTime"] for s in spans)
    fim = max(s["startTime"] + s["duration"] for s in spans)
    raiz = min(spans, key=lambda s: s["startTime"])
    return {
        "id": t["traceID"], "ini": ini / 1e6, "dur_ms": (fim - ini) / 1e3,
        "erro": any(_eh_erro(s.get("tags", [])) for s in spans),
        "servicos": sorted({procs.get(s.get("processID"), {}).get("serviceName", "?") for s in spans}),
        "op": raiz.get("operationName", "?"), "n_spans": len(spans),
    }


def contar(traces, ini, fim, limiar):
    sel = [t for t in traces if ini <= t["ini"] <= fim]
    return {"total": len(sel), "erro": sum(t["erro"] for t in sel),
            "lentos": sum(t["dur_ms"] >= limiar for t in sel)}


# ------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="medicao_log.csv")
    ap.add_argument("--prefixo", default="")
    ap.add_argument("--conds", default="BC", help="condições a coletar (ex.: BC, CP)")
    ap.add_argument("--blocos", type=int, default=5)
    ap.add_argument("--loki", default=None, help="URL do Loki (ex.: http://IP:3100)")
    ap.add_argument("--jaeger", default=None, help="URL do Jaeger (ex.: http://IP:16686)")
    ap.add_argument("--seletor", default='{service_name=~".+"}')
    ap.add_argument("--margem", type=int, default=15, help="s após o fim da falha ainda tratados como falha")
    ap.add_argument("--limiar", type=float, default=500.0, help="duração (ms) de um trace lento")
    ap.add_argument("--limite", type=int, default=5000, help="máx. de traces por serviço e consulta")
    ap.add_argument("--ignorar", default="jaeger-all-in-one,jaeger-query",
                    help="serviços do próprio Jaeger, ignorados (separados por vírgula)")
    ap.add_argument("--detalhe", action="store_true", help="lista cada trace lento")
    a = ap.parse_args()

    # Leitura por posição: nome,tracing,agente,inicio,fim,falha,falha_ini,falha_fim.
    # Funciona mesmo se o cabeçalho do arquivo for o antigo (5 colunas).
    log = {}
    with open(a.log, newline="") as f:
        for lin in csv.reader(f):
            if len(lin) < 5 or not lin[3].strip().isdigit():
                continue  # cabeçalho ou linha inválida
            lin = lin + ["none", "0", "0"][len(lin) - 5:] if len(lin) < 8 else lin
            log[lin[0]] = {"inicio_epoch": lin[3], "fim_epoch": lin[4], "falha": lin[5],
                           "falha_ini": lin[6], "falha_fim": lin[7]}

    if a.loki:
        print("Rótulos no Loki:", ", ".join(loki_rotulos(a.loki)))
        print(f"Seletor de logs usado: {a.seletor}\n")

    ignorar = set(filter(None, a.ignorar.split(",")))
    res = {}  # nome -> dict
    for c in a.conds:
        for b in range(1, a.blocos + 1):
            n = f"{a.prefixo}{c}{b}"
            r = log.get(n)
            if not r:
                print(f"(execução {n} ausente em {a.log})")
                continue
            ini, fim = int(r["inicio_epoch"]), int(r["fim_epoch"])
            fi, ff = int(r["falha_ini"]), int(r["falha_fim"])
            if fi == 0:
                print(f"(execução {n} sem falha registrada: janela DURANTE vazia)")
                fi = ff = fim
            ff_m = min(ff + a.margem, fim)
            d = {"ini": ini, "fim": fim, "fi": fi, "ff": ff_m}
            if a.loki:
                try:
                    d["loki"] = loki_janelas(a.loki, a.seletor, {
                        "antes": (ini, fi), "durante": (fi, ff_m), "depois": (ff_m, fim)})
                except Exception as e:
                    print(f"[{n}] erro no Loki: {e}")
            if a.jaeger:
                try:
                    tr, trunc = jaeger_traces(a.jaeger, ini, fim, a.limite, ignorar)
                    d["traces"] = tr
                    d["trunc"] = trunc
                    d["j_run"] = contar(tr, ini, fim, a.limiar)
                    d["j_falha"] = contar(tr, fi, ff_m, a.limiar)
                    if not tr:
                        print(f"[{n}] AVISO: Jaeger sem nenhum trace na janela "
                              "(retenção de 72 h expirada ou serviços diferentes?)")
                    if trunc:
                        print(f"[{n}] AVISO: consulta atingiu --limite; aumente-o")
                except Exception as e:
                    print(f"[{n}] erro no Jaeger: {e}")
            res[n] = d

    # ---------------------------------------------------------------- tabelas por execução
    if a.jaeger:
        print(f"\n## Jaeger por execução (lento = duração >= {a.limiar:.0f} ms)\n")
        print("| Execução | traces | com erro | lentos | traces na falha | erro na falha | lentos na falha |")
        print("|---|---|---|---|---|---|---|")
        for n, d in res.items():
            if "j_run" in d:
                j, jf = d["j_run"], d["j_falha"]
                print(f"| {n} | {j['total']} | {j['erro']} | {j['lentos']} | "
                      f"{jf['total']} | {jf['erro']} | {jf['lentos']} |")
        if a.detalhe:
            print("\n### Traces lentos (início relativo à execução e à falha)\n")
            for n, d in res.items():
                for t in sorted((t for t in d.get("traces", []) if t["dur_ms"] >= a.limiar),
                                key=lambda t: t["ini"]):
                    print(f"{n}: +{t['ini'] - d['ini']:.0f}s do início "
                          f"({'DURANTE a falha' if d['fi'] <= t['ini'] <= d['ff'] else 'fora da falha'}), "
                          f"{t['dur_ms']:.0f} ms, {t['op']}, {t['n_spans']} spans, "
                          f"serviços: {', '.join(t['servicos'])}{', ERRO' if t['erro'] else ''}")

    if a.loki:
        print("\n## Loki por execução: linhas por minuto (antes / durante / depois da falha)\n")
        print("| Execução | total | erro | keystone | depreciação |")
        print("|---|---|---|---|---|")
        for n, d in res.items():
            if "loki" in d:
                L = d["loki"]
                cel = [" / ".join(f"{L[j][k]:.0f}" for j in ("antes", "durante", "depois"))
                       for k in PADROES]
                print(f"| {n} | " + " | ".join(cel) + " |")

    # ----------------------------------------------------------------- resumo por condição
    if a.loki:
        print("\n## Loki por condição: linhas/min, média ± dp entre execuções\n")
        print("| Condição | janela | total | erro | keystone | depreciação |")
        print("|---|---|---|---|---|---|")
        for c in a.conds:
            runs = [d for n, d in res.items() if n.startswith(f"{a.prefixo}{c}") and "loki" in d]
            for j in ("antes", "durante", "depois"):
                cel = []
                for k in PADROES:
                    m, s = media_dp([r["loki"][j][k] for r in runs])
                    cel.append(f"{m:.0f} ± {s:.0f}")
                print(f"| {NOMES.get(c, c)} (n={len(runs)}) | {j} | " + " | ".join(cel) + " |")

    if a.jaeger:
        print("\n## Jaeger por condição: média ± dp entre execuções (soma entre parênteses)\n")
        print("| Condição | n | traces | com erro | lentos | erro na falha | lentos na falha |")
        print("|---|---|---|---|---|---|---|")
        for c in a.conds:
            runs = [d for n, d in res.items() if n.startswith(f"{a.prefixo}{c}") and "j_run" in d]
            cel = []
            for fonte, k in (("j_run", "total"), ("j_run", "erro"), ("j_run", "lentos"),
                             ("j_falha", "erro"), ("j_falha", "lentos")):
                v = [r[fonte][k] for r in runs]
                m, s = media_dp(v)
                cel.append(f"{m:.1f} ± {s:.1f} ({sum(v)})")
            print(f"| {NOMES.get(c, c)} | {len(runs)} | " + " | ".join(cel) + " |")

        if len(a.conds) == 2:
            x, y = a.conds[0], a.conds[1]
            print(f"\n## Diferença pareada por bloco ({x} − {y}): média [IC 95%]\n")
            print("| Medida | blocos | diferença |")
            print("|---|---|---|")
            blocos = [b for b in range(1, a.blocos + 1)
                      if f"{a.prefixo}{x}{b}" in res and f"{a.prefixo}{y}{b}" in res
                      and "j_run" in res[f"{a.prefixo}{x}{b}"] and "j_run" in res[f"{a.prefixo}{y}{b}"]]
            for rot, fonte, k in (("traces criados", "j_run", "total"), ("lentos", "j_run", "lentos"),
                                  ("lentos na falha", "j_falha", "lentos"), ("com erro", "j_run", "erro")):
                dif = [res[f"{a.prefixo}{x}{b}"][fonte][k] - res[f"{a.prefixo}{y}{b}"][fonte][k] for b in blocos]
                if dif:
                    m, h = ic95(dif)
                    print(f"| {rot} | {len(blocos)} | {m:+.1f} [{m - h:+.1f}; {m + h:+.1f}] |")

    # falhas do Locust, como contexto para os traces com erro
    try:
        from resumir_medicao import resumo_locust
        falhas = {}
        for n in res:
            try:
                falhas[n] = resumo_locust(f"overhead_{n}_stats.csv")["falhas"]
            except Exception:
                pass
        if falhas:
            print("\nFalhas de requisição (Locust) por execução:", falhas)
    except ImportError:
        pass


if __name__ == "__main__":
    sys.exit(main())
