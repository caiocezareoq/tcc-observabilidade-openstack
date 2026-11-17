#!/usr/bin/env bash
# Troca a configuração do AGENTE local (rodar no OpenStack), recria o contêiner, confere e espera.
#
# Uso:  ./trocar_agente.sh B        # padrão    <- otel-collector-agent-config.PADRAO.yaml
#       ./trocar_agente.sh C        # otimizada <- otel-collector-agent-config.OTIM.yaml
#       ./trocar_agente.sh D        # agente otimizado (como C) + gateway D (tail sampling, decision_wait 30 s)
#       ./trocar_agente.sh C -f     # força a troca mesmo que o agente já esteja na condição
#
# Variáveis opcionais:
#   AGENTE_DIR=~/observability-agent       pasta do docker-compose do agente
#   ESPERA=90                              pausa (s) depois da troca, antes da próxima execução
#   GATEWAY_SSH=ubuntu@IP_DO_SERVIDOR       se definida, troca também o gateway na AWS por SSH
#   GATEWAY_SSH_KEY=~/labsuser.pem         chave .pem do SSH (chmod 400)
#   GATEWAY_DIR=observabillity-stack       pasta do docker-compose na AWS (relativa ao $HOME)
#   CONT=otel-collector-agent              nome do contêiner do agente
#
# Ordem: confere se já está na condição; valida PADRAO e OTIM; testa o SSH; troca o gateway;
# copia a origem para otel-collector-agent-config.yaml; RECRIA o contêiner (docker compose up
# --force-recreate: o restart não enxerga um arquivo novo); espera o "Everything is ready";
# confere a configuração que o contêiner realmente usa; faz a pausa.
set -u
COND="${1:?uso: $0 B|C|D [-f]}"
case "$COND" in B|C|D) ;; *) echo "uso: $0 B|C|D [-f]"; exit 1;; esac
FORCAR=0; [ "${2:-}" = "-f" ] && FORCAR=1
# D usa o MESMO agente de C; só o gateway muda. A conferência do agente é a de C.
AG="$COND"; [ "$COND" = D ] && { AG=C; FORCAR=1; }
AQUI="$(cd "$(dirname "$0")" && pwd)"
CONT="${CONT:-otel-collector-agent}"
ESPERA="${ESPERA:-90}"
GATEWAY_DIR="${GATEWAY_DIR:-observabillity-stack}"
AGENTE_DIR="${AGENTE_DIR:-$HOME/observability-agent}"
AGENTE_DIR="${AGENTE_DIR/#\~/$HOME}"

erro() { echo "ERRO: $*" >&2; exit 1; }
otimizada() { grep -q 'filter/logs_noise' "$1" && grep -q -E 'compression: *gzip' "$1"; }

SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes)
if [ -n "${GATEWAY_SSH_KEY:-}" ]; then
  GATEWAY_SSH_KEY="${GATEWAY_SSH_KEY/#\~/$HOME}"
  [ -f "$GATEWAY_SSH_KEY" ] || erro "chave $GATEWAY_SSH_KEY não encontrada"
  SSH_OPTS+=(-o IdentitiesOnly=yes -i "$GATEWAY_SSH_KEY")
fi

if [ "$FORCAR" = 0 ] && "$AQUI/conferir_agente.sh" "$AG" > /dev/null 2>&1; then
  echo "O agente já está na condição $COND (conferido no contêiner). Nada a trocar; use -f para forçar."
  exit 0
fi

# Arquivos
[ -f "$AGENTE_DIR/docker-compose.yml" ] || erro "não achei $AGENTE_DIR/docker-compose.yml (use AGENTE_DIR=...)"
ARQ="$AGENTE_DIR/otel-collector-agent-config.yaml"
PAD="$AGENTE_DIR/otel-collector-agent-config.PADRAO.yaml"
OTI="$AGENTE_DIR/otel-collector-agent-config.OTIM.yaml"
grep -q 'otel-collector-agent-config.yaml' "$AGENTE_DIR/docker-compose.yml" \
  || erro "o docker-compose.yml não monta otel-collector-agent-config.yaml"
[ -f "$PAD" ] || erro "$PAD não existe"
[ -f "$OTI" ] || erro "$OTI não existe"
otimizada "$PAD" && erro "$PAD tem as marcas da otimização (filter/logs_noise e gzip); não é o padrão"
otimizada "$OTI" || erro "$OTI não tem as marcas da otimização (filter/logs_noise e gzip)"
# placeholders não preenchidos (linhas de comentário são ignoradas)
for f in "$PAD" "$OTI"; do
  grep -v '^[[:space:]]*#' "$f" | grep -qE 'SEU_IP|SUA_SENHA' \
    && erro "há placeholder sem preencher (SEU_IP... ou SUA_SENHA...) fora de comentário em $f"
done
[ "$AG" = B ] && SRC="$PAD" || SRC="$OTI"

# Se o gateway será trocado por SSH, testa a conexão ANTES de mexer em qualquer coisa
if [ -n "${GATEWAY_SSH:-}" ]; then
  ssh "${SSH_OPTS[@]}" "$GATEWAY_SSH" true 2>/dev/null \
    || erro "não consegui conectar em $GATEWAY_SSH por SSH (chave .pem em GATEWAY_SSH_KEY? permissão 400? instância ligada? Tailscale conectada?)"
  ssh "${SSH_OPTS[@]}" "$GATEWAY_SSH" "test -x ~/$GATEWAY_DIR/trocar_gateway.sh" 2>/dev/null \
    || erro "não achei ~/$GATEWAY_DIR/trocar_gateway.sh executável na AWS"
fi

# Protege um arquivo ativo que não seja nem o padrão nem o otimizado
if [ -d "$ARQ" ]; then
  erro "$ARQ é uma PASTA (o Docker a criou quando o arquivo não existia). Pare o contêiner e remova a pasta: (cd $AGENTE_DIR && docker compose down && sudo rm -rf otel-collector-agent-config.yaml), e rode de novo"
fi
if [ -f "$ARQ" ] && ! cmp -s "$ARQ" "$PAD" && ! cmp -s "$ARQ" "$OTI"; then
  BK="$ARQ.bak.$(date +%Y%m%d-%H%M%S)"; cp "$ARQ" "$BK"
  echo "AVISO: o arquivo ativo não era igual ao PADRAO nem ao OTIM; cópia salva em $BK"
fi
[ -f "$ARQ" ] || echo "AVISO: $ARQ não existia (o contêiner usava uma cópia antiga); será criado."

# Gateway
if [ -n "${GATEWAY_SSH:-}" ]; then
  echo "Trocando o gateway para $COND em $GATEWAY_SSH ..."
  ssh "${SSH_OPTS[@]}" "$GATEWAY_SSH" "cd ~/$GATEWAY_DIR && ./trocar_gateway.sh $COND" \
    || erro "falha ao trocar o gateway por SSH. Troque-o à mão (./trocar_gateway.sh $COND na AWS) e rode de novo com -f"
else
  echo ">>> LEMBRETE: troque o GATEWAY na AWS para $COND (./trocar_gateway.sh $COND) se ainda não o fez."
fi

# Agente: copia e RECRIA o contêiner
cp -f "$SRC" "$ARQ" || erro "falha ao copiar $SRC para $ARQ"
echo "Aplicado $(basename "$SRC") em $ARQ. Recriando $CONT ..."
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if docker compose version > /dev/null 2>&1; then DC=(docker compose); else DC=(docker-compose); fi
(cd "$AGENTE_DIR" && "${DC[@]}" up -d --force-recreate) || erro "docker compose up falhou"
PRONTO=0
for i in $(seq 1 30); do
  sleep 2
  if docker logs --since "$T0" "$CONT" 2>&1 | grep -q 'Everything is ready'; then PRONTO=1; break; fi
done
[ "$PRONTO" = 1 ] || { docker logs --tail 15 "$CONT" 2>&1; erro "o agente não ficou pronto em 60 s (veja os logs acima)"; }
echo "Agente recriado e pronto."

"$AQUI/conferir_agente.sh" "$AG" || erro "conferência do agente falhou depois da troca"

echo "Pausa de ${ESPERA}s antes da próxima execução (estabilização do agente e do gateway)..."
for ((s=ESPERA; s>0; s-=10)); do
  printf '  faltam %ss\n' "$s"; sleep $(( s < 10 ? s : 10 ))
done
echo "Pronto: condição $COND ativa no agente. Próximo passo: ./rodar_medicao.sh NOME on [keystone|stress]"
