#!/usr/bin/env bash
# Confere o AGENTE local (rodar no OpenStack) lendo a configuração que o CONTÊINER realmente usa.
# Uso:  ./conferir_agente.sh B   # padrão
#       ./conferir_agente.sh C   # otimizada
# Lê o arquivo de dentro do contêiner (docker cp), não só o do disco: se o arquivo do host foi
# substituído sem recriar o contêiner, o contêiner segue com a versão antiga e isto acusa.
COND="${1:?uso: $0 B|C}"
CONT="${CONT:-otel-collector-agent}"
docker ps --format '{{.Names}}' | grep -qx "$CONT" || { echo "ERRO: contêiner $CONT não está rodando"; exit 1; }
MNT=$(docker inspect "$CONT" --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' \
      | awk -F'|' '$2 ~ /\.(yaml|yml)$/ {print; exit}')
[ -n "$MNT" ] || { echo "ERRO: o contêiner $CONT não tem arquivo .yaml montado"; docker inspect "$CONT" --format '{{json .Mounts}}'; exit 1; }
ARQ="${MNT%%|*}"; DEST="${MNT##*|}"
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
docker cp "$CONT:$DEST" - 2>/dev/null | tar -xO > "$TMP" 2>/dev/null
[ -s "$TMP" ] || { echo "ERRO: não consegui ler $DEST dentro do contêiner"; exit 1; }
echo "Configuração em uso no contêiner ($DEST); arquivo no host: $ARQ"
OK=1
tem() { grep -q -E "$1" "$TMP"; }
if [ "$COND" = C ]; then
  tem 'filter/logs_noise' || { echo "FALTA filter/logs_noise (filtragem de logs)"; OK=0; }
  tem 'compression: *gzip' || { echo "FALTA compression: gzip"; OK=0; }
  tem 'timeout: *10s' || { echo "FALTA batch timeout: 10s"; OK=0; }
  tem 'collection_interval: *30s' || { echo "FALTA collection_interval: 30s"; OK=0; }
else
  tem 'filter/logs_noise' && { echo "SOBRA filter/logs_noise (isso é da otimizada)"; OK=0; }
  tem 'compression: *gzip' && { echo "SOBRA compression: gzip (isso é da otimizada)"; OK=0; }
  tem 'collection_interval: *30s' && { echo "SOBRA collection_interval: 30s (isso é da otimizada)"; OK=0; }
fi
if [ ! -f "$ARQ" ]; then
  echo "AVISO: o arquivo do host ($ARQ) não existe; o contêiner usa uma cópia antiga dele."; OK=0
elif ! cmp -s "$ARQ" "$TMP"; then
  echo "AVISO: o arquivo do host difere do que o contêiner enxerga (recrie o contêiner: docker compose up -d --force-recreate)."; OK=0
fi
[ "$OK" = 1 ] && echo "OK: agente coerente com a condição $COND" || { echo "ERRO: agente NÃO está na condição $COND"; exit 1; }
