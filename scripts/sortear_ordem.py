"""
Sorteia a ordem das execuções da rodada com repetição.

Desenho: blocos aleatorizados. Cada bloco contém uma execução de cada condição
(A = sem observabilidade, B = padrão, C = otimizada), em ordem sorteada. Assim,
qualquer deriva do ambiente ao longo das horas (memória, cache, temperatura)
atinge as três condições de forma equilibrada, e a comparação entre condições
pode ser feita dentro de cada bloco (pareada).

Uso:  python3 sortear_ordem.py [blocos=5] [semente=2026] [condicoes=ABC] [prefixo=]
Para os Cenários 2 e 3 (só padrão x otimizada):  python3 sortear_ordem.py 5 2027 BC c2_
A semente é fixa de propósito: registre-a no texto para que a ordem seja reprodutível.
"""
import random
import sys

blocos = int(sys.argv[1]) if len(sys.argv) > 1 else 5
semente = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
conds = list(sys.argv[3]) if len(sys.argv) > 3 else ["A", "B", "C"]
prefixo = sys.argv[4] if len(sys.argv) > 4 else ""
rng = random.Random(semente)

print(f"Semente {semente}, {blocos} blocos\n")
n = 0
for b in range(1, blocos + 1):
    ordem = list(conds)
    rng.shuffle(ordem)
    print(f"Bloco {b}: " + " -> ".join(f"{prefixo}{c}{b}" for c in ordem))
    n += len(conds)
print(f"\nTotal: {n} execuções. Rode ./rodar_medicao.sh NOME on|off na ordem acima "
      "(A usa off com o agente parado; B e C usam on com a configuração correspondente).")
