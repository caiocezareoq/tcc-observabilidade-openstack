#!/usr/bin/env bash
# Roda UMA execução de medição (Cenário 1, 20 usuários, 5 min) com sar em paralelo.
# Uso:  ./rodar_medicao.sh NOME on|off [none|keystone|stress]
#   3º argumento (opcional): falha injetada (Cenário 2 = keystone, Cenário 3 = stress).
#   Variáveis opcionais: FALHA_APOS (s até injetar, padrão 120) e FALHA_DUR (s, padrão 60).
#   CONFIRME os comandos de injeção abaixo com os que você usou em setembro.
#   NOME  : A1, B1, C1, A2 ... (define runNOME.sa e overhead_NOME_*)
#   on|off: TRACING (off = sem cabeçalho OSProfiler; use com o agente PARADO)
# Antes: source script.sh   (variáveis de autenticação e IDs do OpenStack)
set -u
NOME="${1:?uso: $0 NOME on|off}"
MODO="${2:?uso: $0 NOME on|off}"
FALHA="${3:-none}"
FALHA_APOS="${FALHA_APOS:-120}"
FALHA_DUR="${FALHA_DUR:-60}"
case "$FALHA" in none|keystone|stress) ;; *) echo "ERRO: falha deve ser none, keystone ou stress"; exit 1;; esac
case "$MODO" in on|off) ;; *) echo "ERRO: o segundo argumento deve ser on ou off"; exit 1;; esac

cd "$HOME" || exit 1

erro() { echo "ERRO: $*"; exit 1; }

[ -f locustfile.py ] || erro "locustfile.py não está em $HOME"
grep -q TRACING locustfile.py || erro "locustfile.py é a versão antiga (sem o interruptor TRACING)"

for v in OS_PASSWORD GLANCE_IMAGE_ID NOVA_FLAVOR_ID NEUTRON_NETWORK_ID; do
  [ -n "${!v:-}" ] || erro "variável $v vazia. Rode: source script.sh"
done
if [ "$MODO" = "on" ]; then
  [ -n "${OSPROFILER_HMAC_KEY:-}" ] || erro "OSPROFILER_HMAC_KEY vazia. Rode: source script.sh"
fi

pgrep -x sar > /dev/null && erro "já existe um sar rodando (pgrep -a sar)"

AGENTE_ON=0
docker ps --format '{{.Names}}' | grep -qx otel-collector-agent && AGENTE_ON=1
if [ "$MODO" = "off" ] && [ "$AGENTE_ON" = 1 ]; then erro "TRACING=off, mas o agente OTel está rodando. Pare-o antes."; fi
if [ "$MODO" = "on" ]  && [ "$AGENTE_ON" = 0 ]; then erro "TRACING=on, mas o agente OTel está parado. Suba-o antes."; fi

for f in "run${NOME}.sa" "overhead_${NOME}_stats.csv" "overhead_${NOME}.html"; do
  [ -e "$f" ] && erro "$f já existe. Use outro NOME ou mova o arquivo."
done

QTD=$(openstack server list -f value -c ID 2>/dev/null | wc -l)
echo "Instâncias existentes: $QTD"
free -h | sed -n '1,2p'
echo "Iniciando $NOME (TRACING=$MODO, agente ligado=$AGENTE_ON) em $(date '+%H:%M:%S'), pasta $(pwd)"

export TRACING="$MODO"
T_INI=$(date +%s)
LC_ALL=C sar -o "run${NOME}.sa" 5 66 > /dev/null 2>&1 &
SAR_PID=$!

locust -f locustfile.py --host=http://192.168.0.250 --users 20 --spawn-rate 5 \
  --run-time 5m --headless --csv="overhead_${NOME}" --html="overhead_${NOME}.html" &
LOCUST_PID=$!

T_F_INI=0; T_F_FIM=0
if [ "$FALHA" != none ]; then
  sleep "$FALHA_APOS"
  T_F_INI=$(date +%s); echo "[falha] $FALHA injetada em $(date '+%H:%M:%S')"
  if [ "$FALHA" = keystone ]; then
    docker stop keystone > /dev/null                    # mesmo comando das execuções de setembro
    sleep "$FALHA_DUR"
    docker start keystone > /dev/null
  else
    stress-ng --cpu 4 --vm 2 --vm-bytes 1G --timeout "${FALHA_DUR}s" > /dev/null 2>&1
  fi
  T_F_FIM=$(date +%s); echo "[falha] $FALHA encerrada em $(date '+%H:%M:%S')"
fi
wait "$LOCUST_PID"
RC=$?

if [ ! -s "overhead_${NOME}_stats.csv" ]; then
  kill "$SAR_PID" 2>/dev/null
  mv "run${NOME}.sa" "run${NOME}.sa.invalida" 2>/dev/null
  erro "o Locust não gerou overhead_${NOME}_stats.csv (código $RC). Execução $NOME invalidada."
fi

wait "$SAR_PID"
T_FIM=$(date +%s)
[ -f medicao_log.csv ] || echo "nome,tracing,agente_ligado,inicio_epoch,fim_epoch,falha,falha_ini,falha_fim" > medicao_log.csv
echo "${NOME},${MODO},${AGENTE_ON},${T_INI},${T_FIM},${FALHA},${T_F_INI},${T_F_FIM}" >> medicao_log.csv
echo "Concluída a execução $NOME em $(date '+%H:%M:%S'). Arquivos:"
ls -la "run${NOME}.sa" "overhead_${NOME}_stats.csv" "overhead_${NOME}.html"
