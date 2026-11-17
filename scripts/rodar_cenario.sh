#!/usr/bin/env bash
# Roda UM CENÁRIO INTEIRO (10 execuções, B e C em blocos sorteados), de forma automática.
#
# Uso:  ./rodar_cenario.sh 1|2|3|4|5|6|7 [-n]    (-n = só mostra o plano, sem executar nada)
#   1 = sem falha (semente 2030, prefixo n1_)      2 = parada do Keystone (2031, n2_)
#   3 = saturação de CPU (2032, n3_)
#   5 = B x D PAREADO, sem falha (semente 2033, prefixo n5_): 5 blocos sorteados, custo do servidor
#       com o tail sampling de decision_wait 30 s contra o padrão
#   6 = B x D PAREADO, Cenário 2 (parada do Keystone; semente 2034, prefixo n6_)
#   7 = B x D PAREADO, Cenário 3 (saturação de CPU; semente 2035, prefixo n7_)
#       (6 e 7 comparam, no MESMO dia e em blocos sorteados, a retenção de traces e o custo de D contra B)
#   4 = TESTE do decision_wait: Cenário 2 (Keystone), 5 execuções D (tail sampling com
#       decision_wait 30 s, agente otimizado), prefixo w2_, sem sorteio
#
# Antes: source script.sh (credenciais do OpenStack) e copiar para o $HOME: rodar_medicao.sh,
# trocar_agente.sh, conferir_agente.sh, verificar_amostragem.py, sortear_ordem.py, locustfile.py.
# Variáveis:
#   GATEWAY_SSH=ubuntu@IP_DO_SERVIDOR  GATEWAY_SSH_KEY=~/labsuser.pem   troca o gateway sozinho
#       (sem GATEWAY_SSH, o script PARA e pede que você troque o gateway à mão a cada mudança)
#   PROM=http://IP_DO_SERVIDOR:9090      Prometheus (verificação do gateway; padrão acima)
#   PAUSA_ENTRE=60                      pausa (s) entre execuções da MESMA condição
#
# Para cada execução, na ordem sorteada:
#   1. apaga as instâncias remanescentes e espera a remoção;
#   2. se a condição mudou (ou é a primeira desta chamada): troca gateway + agente, recria o
#      contêiner, confere e espera 90 s; senão, só espera PAUSA_ENTRE;
#   3. roda a medição, e o próprio rodar_medicao.sh confere no Prometheus que o gateway estava
#      com a amostragem certa (B = nenhuma, C = tail sampling).
# Se algo falhar, PARA (nunca repete sozinho). Corrija e rode de novo: as execuções já
# registradas no medicao_log.csv são puladas, e a execução com problema recomeça. Se ela deixou
# arquivos, mova runNOME.sa e overhead_NOME_* para descartadas/ e apague a linha do log antes.
set -u
N="${1:?uso: $0 1|2|3|4|5|6|7 [-n]}"
DRY=0; [ "${2:-}" = "-n" ] && DRY=1
case "$N" in
  1) SEMENTE=2030; PREF=n1_; FALHA=none ;;
  2) SEMENTE=2031; PREF=n2_; FALHA=keystone ;;
  3) SEMENTE=2032; PREF=n3_; FALHA=stress ;;
  4) SEMENTE=-;    PREF=w2_; FALHA=keystone ;;
  5) SEMENTE=2033; PREF=n5_; FALHA=none ;;
  6) SEMENTE=2034; PREF=n6_; FALHA=keystone ;;
  7) SEMENTE=2035; PREF=n7_; FALHA=stress ;;
  *) echo "uso: $0 1|2|3|4|5|6|7 [-n]"; exit 1 ;;
esac
PROM="${PROM:-http://IP_DO_SERVIDOR:9090}"
PAUSA_ENTRE="${PAUSA_ENTRE:-60}"
cd "$HOME" || exit 1
erro() { echo "ERRO: $*" >&2; exit 1; }

for f in rodar_medicao.sh trocar_agente.sh conferir_agente.sh verificar_amostragem.py sortear_ordem.py locustfile.py; do
  [ -f "$f" ] || erro "falta $HOME/$f"
done
if [ "$N" = 4 ]; then
  ORDEM="w2_D1 w2_D2 w2_D3 w2_D4 w2_D5"; NEXEC=5
else
  CONDS=BC; { [ "$N" = 5 ] || [ "$N" = 6 ] || [ "$N" = 7 ]; } && CONDS=BD
  ORDEM=$(python3 sortear_ordem.py 5 "$SEMENTE" "$CONDS" "$PREF" | grep -o "${PREF}[A-Z][0-9]*"); NEXEC=10
fi
[ "$(echo "$ORDEM" | wc -w)" = "$NEXEC" ] || erro "o sorteio não devolveu $NEXEC execuções"

if [ "$DRY" = 1 ]; then
  echo "Plano do Cenário $N (semente $SEMENTE, falha: $FALHA):"
  ANT=""
  for NOME in $ORDEM; do
    COND="${NOME:${#PREF}:1}"
    JA=""; grep -q "^${NOME}," medicao_log.csv 2>/dev/null && JA="  [já feita: será pulada]"
    TROCA=""; [ -z "$JA" ] && { [ "$COND" != "$ANT" ] && TROCA="  [troca para $COND + 90 s]"; ANT="$COND"; }
    echo "  $NOME  (condição $COND)$TROCA$JA"
  done
  exit 0
fi

for v in OS_PASSWORD GLANCE_IMAGE_ID NOVA_FLAVOR_ID NEUTRON_NETWORK_ID OSPROFILER_HMAC_KEY; do
  [ -n "${!v:-}" ] || erro "variável $v vazia. Rode: source script.sh"
done

limpar_instancias() {
  echo "Removendo instâncias remanescentes ..."
  local ids="" ok=0 i
  for i in 1 2 3 4 5 6; do   # o Keystone pode estar voltando de uma falha
    if ids=$(openstack server list -f value -c ID 2>/dev/null); then ok=1; break; fi
    sleep 10
  done
  [ "$ok" = 1 ] || erro "não consegui listar as instâncias (OpenStack/Keystone fora do ar?)"
  [ -n "$ids" ] && echo "$ids" | xargs -r openstack server delete
  for i in $(seq 1 45); do
    [ "$(openstack server list -f value -c ID 2>/dev/null | wc -l)" = 0 ] && { echo "Sem instâncias."; return 0; }
    sleep 4
  done
  erro "ainda há instâncias depois de 3 min; verifique com 'openstack server list'"
}

ANT=""
for NOME in $ORDEM; do
  COND="${NOME:${#PREF}:1}"
  if grep -q "^${NOME}," medicao_log.csv 2>/dev/null; then echo "Pulando $NOME (já está no medicao_log.csv)."; continue; fi
  echo; echo "==================== $NOME  (condição $COND, falha: $FALHA)  $(date '+%H:%M:%S') ===================="
  limpar_instancias
  if [ "$COND" != "$ANT" ]; then
    FORCA=""; [ -z "$ANT" ] && FORCA="-f"    # primeira desta chamada: não assume o estado atual
    if [ -z "${GATEWAY_SSH:-}" ]; then
      read -r -p ">>> Troque o GATEWAY na AWS para $COND (./trocar_gateway.sh $COND) e pressione Enter: " _
    fi
    ESPERA=90 ./trocar_agente.sh "$COND" $FORCA || erro "falha ao trocar para $COND; nada foi medido em $NOME"
  else
    echo "Mesma condição da execução anterior; pausa de ${PAUSA_ENTRE}s."; sleep "$PAUSA_ENTRE"
  fi
  ESP="$COND"; [ "$COND" = D ] && ESP=C    # D também deve ter tail sampling e nenhum probabilístico
  PROM="$PROM" ESPERADO="$ESP" ./rodar_medicao.sh "$NOME" on "$FALHA" \
    || erro "a execução $NOME falhou ou o gateway estava errado. Descarte-a (veja o cabeçalho) e rode de novo ./rodar_cenario.sh $N"
  ANT="$COND"
done
echo; echo "Cenário $N concluído ($(date '+%H:%M'))."
if [ "$N" = 5 ]; then
  echo "Próximo: python3 resumir_rodada.py --iface enp5s0 --skip 2 --conds BD --prefixo n5_ --prom $PROM --log medicao_log.csv | tee resumo_n5.txt"
elif [ "$N" = 6 ] || [ "$N" = 7 ]; then
  echo "Próximo: python3 resumir_rodada.py --iface enp5s0 --skip 2 --conds BD --prefixo $PREF --prom $PROM --log medicao_log.csv | tee resumo_${PREF%_}.txt"
  echo "         python3 coletar_qa2.py --log medicao_log.csv --prefixo $PREF --conds BD --detalhe --loki http://IP_DO_SERVIDOR:3100 --jaeger http://IP_DO_SERVIDOR:16686 | tee qa2_${PREF%_}.txt"
  echo "         (rode o coletar_qa2.py LOGO: o Jaeger guarda os traces por 72 h)"
elif [ "$N" = 4 ]; then
  echo "Próximo: python3 coletar_qa2.py --log medicao_log.csv --prefixo w2_ --conds D --detalhe --loki http://IP_DO_SERVIDOR:3100 --jaeger http://IP_DO_SERVIDOR:16686 | tee qa2_w2.txt"
else
  echo "Próximo: python3 resumir_rodada2.py --iface enp5s0 --skip 2 --conds BC --prefixo $PREF --prom $PROM --log medicao_log.csv"
fi
