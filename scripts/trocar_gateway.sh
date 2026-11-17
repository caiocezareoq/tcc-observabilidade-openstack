#!/usr/bin/env bash
# Troca a configuração do GATEWAY (rodar NA AWS, na pasta do docker-compose).
# Uso:  ./trocar_gateway.sh B   # padrão (sem amostragem)        <- otel-collector-config.PADRAO.yaml
#       ./trocar_gateway.sh C   # otimizada, tail sampling        <- otel-collector-config.TAIL.yaml
#       ./trocar_gateway.sh P   # otimizada, probabilística 10%   <- otel-collector-config.PROB.yaml
#       ./trocar_gateway.sh D   # como C, com decision_wait 30 s  <- otel-collector-config.TAIL30.yaml
#                                 (teste da hipótese dos traces longos; criado a partir do TAIL.yaml
#                                  se não existir: só troca decision_wait: 10s por 30s)
# Os arquivos de origem precisam existir. O script confere o pipeline de traces de cada um antes
# de copiar, recria o contêiner (o gateway só relê o arquivo ao ser recriado) e, depois, confere
# a configuração que o CONTÊINER realmente enxerga (docker cp), não só a do disco.
set -eu
cd "$(dirname "$0")"
COND="${1:?uso: $0 B|C|P|D}"
CONT="${CONT:-otel-collector-gateway}"
EXTRA=""
case "$COND" in
  B) SRC=otel-collector-config.PADRAO.yaml; ESPERA='processors: \[batch\]'
     # aceita também o nome com til (PADRÃO), usado nas rodadas anteriores
     [ -f "$SRC" ] || { [ -f "otel-collector-config.PADRÃO.yaml" ] && cp "otel-collector-config.PADRÃO.yaml" "$SRC" && echo "Criado $SRC a partir de PADRÃO.yaml"; } ;;
  C) SRC=otel-collector-config.TAIL.yaml;   ESPERA='processors: \[tail_sampling, batch\]'
     EXTRA='decision_wait: *10s' ;;
  P) SRC=otel-collector-config.PROB.yaml;   ESPERA='processors: \[probabilistic_sampler, batch\]' ;;
  D) SRC=otel-collector-config.TAIL30.yaml; ESPERA='processors: \[tail_sampling, batch\]'
     EXTRA='decision_wait: *30s'
     if [ ! -f "$SRC" ]; then
       [ -f otel-collector-config.TAIL.yaml ] || { echo "ERRO: falta otel-collector-config.TAIL.yaml para criar o TAIL30"; exit 1; }
       grep -q -E 'decision_wait: *10s' otel-collector-config.TAIL.yaml \
         || { echo "ERRO: o TAIL.yaml não tem 'decision_wait: 10s'; crie o TAIL30.yaml à mão"; exit 1; }
       sed -E 's/decision_wait: *10s/decision_wait: 30s/' otel-collector-config.TAIL.yaml > "$SRC"
       echo "Criado $SRC a partir do TAIL.yaml (decision_wait 10s -> 30s)"
     fi ;;
  *) echo "uso: $0 B|C|P|D"; exit 1 ;;
esac
[ -f "$SRC" ] || { echo "ERRO: $SRC não existe"; exit 1; }
# pipeline de traces do arquivo de origem
TRACES=$(awk '/^    traces:/{f=1} f&&/processors:/{print; exit}' "$SRC")
echo "$TRACES" | grep -q -E "$ESPERA" || { echo "ERRO: $SRC tem '$TRACES' no pipeline de traces; esperado $ESPERA"; exit 1; }
if [ -n "$EXTRA" ]; then
  grep -q -E "$EXTRA" "$SRC" || { echo "ERRO: $SRC não tem '$EXTRA'"; exit 1; }
fi
cp "$SRC" otel-collector-config.yaml
docker compose up -d --force-recreate otel-collector
sleep 15
docker logs --tail 5 "$CONT" 2>&1 | tail -3
echo "Condição $COND ativa. Pipeline de traces em uso:"
awk '/^    traces:/{f=1} f&&/processors:/{print; exit}' otel-collector-config.yaml
echo "Contêiner iniciado em: $(docker inspect "$CONT" --format '{{.State.StartedAt}}')"

# Conferência final: o arquivo que o CONTÊINER enxerga é o mesmo do disco?
MNT=$(docker inspect "$CONT" --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' \
      | awk -F'|' '$2 ~ /\.(yaml|yml)$/ {print; exit}')
if [ -z "$MNT" ]; then
  echo "AVISO: não achei arquivo .yaml montado em $CONT; não pude conferir por dentro."
else
  DEST="${MNT##*|}"; TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
  docker cp "$CONT:$DEST" - 2>/dev/null | tar -xO > "$TMP" 2>/dev/null || true
  [ -s "$TMP" ] || { echo "ERRO: não consegui ler $DEST dentro do contêiner"; exit 1; }
  cmp -s "$TMP" "$SRC" || { echo "ERRO: o contêiner NÃO está usando $SRC (arquivo difere do disco). Recrie: docker compose up -d --force-recreate otel-collector"; exit 1; }
  echo "Conferido no contêiner ($DEST): igual a $SRC. $(grep -E 'decision_wait' "$TMP" | head -1 | sed 's/^ *//')"
fi
