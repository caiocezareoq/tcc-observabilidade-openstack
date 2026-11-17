# Observabilidade com OpenTelemetry em um nó OpenStack: scripts, configurações e dados

Material de apoio do TCC **"Gerência de Desempenho e Falhas no OpenStack através da Observabilidade com
OpenTelemetry"** (Caio Cezar Sousa de Oliveira, Redes de Computadores, UFC Quixadá, 2026).

O trabalho avaliou, em um OpenStack *All-in-One* (Kolla-Ansible) com um agente OpenTelemetry Collector local
e um servidor de observabilidade na AWS (Collector *gateway*, Prometheus, Loki, Jaeger e Grafana), três
questões: o efeito de uma configuração otimizada da coleta (QA1), a capacidade de detectar e localizar falhas
induzidas (QA2) e o custo da observabilidade sobre o nó (QA3). Este repositório reúne o que é necessário para
repetir o protocolo e conferir os números das tabelas do Capítulo 6.

## Estrutura

```
scripts/    carga, execução das rodadas, troca de condição, coleta e análise
configs/
  agente/      docker-compose e configurações do Collector no nó (padrão e otimizada)
  servidor/    docker-compose, Prometheus, Loki, Grafana, Alertmanager e configurações do gateway
dados/
  execucoes/   por execução: run<NOME>.sa (sar) e overhead_<NOME>_stats.csv / _failures.csv (Locust)
  resumos/     saídas dos scripts de resumo, de detecção e de retenção de traces
  medicao_log.csv, medicao_log_c1.csv   registro de cada execução (horários e janela da falha)
```

## Condições e cenários

| Condição | Descrição |
|---|---|
| A | referência: agente parado, sem rastreamento por requisição |
| B | configuração padrão do agente, *gateway* sem amostragem |
| P | agente otimizado, amostragem probabilística de 10% no *gateway* (rodada preliminar) |
| C | agente otimizado, *tail sampling* com `decision_wait` de 10 s |
| D | agente otimizado, *tail sampling* com `decision_wait` de 30 s |

| Cenário | Descrição |
|---|---|
| 1 | sem falha |
| 2 | parada do Keystone por 60 s (`docker stop keystone`), a partir de 120 s |
| 3 | saturação de CPU e memória no nó (`stress-ng`), por 60 s, a partir de 120 s |

Carga em todas as execuções: Locust, 20 usuários, entrada de 5 usuários por segundo, 5 minutos.

## Rodadas e nomes das execuções

| Rodada | Data | Prefixo dos arquivos | Condições |
|---|---|---|---|
| Preliminar, Cenário 1 | 12/10/2026 | `A1`–`A5`, `B1`–`B5`, `C1`–`C5` (o `C` é a condição P) | A, B, P |
| Preliminar, Cenário 2 | 15/10/2026 | `c2_B*`, `c2_C*` (o `C` é a condição P) | B, P |
| Preliminar, Cenário 3 | 15/10/2026 | `c3_B*`, `c3_C*` (o `C` é a condição P) | B, P |
| Validação | 23/10/2026 | `n1_*`, `n2_*`, `n3_*` | B, C |
| Ajuste do `decision_wait` | 26/10/2026 | `w2_D*` | D |
| Pareadas B × D | 26 e 27/10/2026 | `n5_*` (Cenário 1), `n6_*` (Cenário 2), `n7_*` (Cenário 3) | B, D |

Cada condição de cada cenário teve 5 execuções, em 5 blocos aleatorizados com semente fixa. Total: 100
execuções válidas.

Observações sobre o registro das medições:

- `medicao_log_c1.csv` guarda o Cenário 1 da rodada preliminar. `medicao_log.csv` guarda as demais rodadas.
- `c2_B1` e `c2_C1` aparecem mais de uma vez no `medicao_log.csv`: o Cenário 2 foi tentado em 12/10 e refeito
  em 15/10. As análises usam a última ocorrência de cada nome (a de 15/10).
- A linha `teste_T1` é uma execução de teste e não entra em nenhuma análise.

## Como repetir uma rodada

Requisitos: nó OpenStack com o agente OpenTelemetry Collector (`docker compose`), servidor com o *gateway* e o
Prometheus, `locust`, `sar` (sysstat), `stress-ng` e Python 3.

1. Defina as variáveis de ambiente (nenhuma credencial é gravada nos arquivos):

   ```
   export OS_PASSWORD=...              # senha do usuário admin do Keystone
   export OSPROFILER_HMAC_KEY=...      # osprofiler_secret do /etc/kolla/passwords.yml
   export GLANCE_IMAGE_ID=... NOVA_FLAVOR_ID=... NEUTRON_NETWORK_ID=...
   export PROM=http://<IP do servidor>:9090
   export GATEWAY_SSH=ubuntu@<IP do servidor>
   export GATEWAY_SSH_KEY=~/chave.pem  # chmod 400
   export DB_PASSWORD=...              # senha do usuário do MariaDB lido pelo agente (receiver mysql)
   export GRAFANA_ADMIN_PASSWORD=...   # senha do admin do Grafana (docker-compose do servidor)
   ```

2. Copie os scripts para o `$HOME` do nó. Os arquivos de origem que `trocar_agente.sh` e
   `trocar_gateway.sh` esperam estão em `configs/agente/` (`otel-collector-agent-config.PADRAO.yaml` e
   `.OTIM.yaml`) e em `configs/servidor/` (`otel-collector-config.PADRAO.yaml`, `.TAIL.yaml`, `.TAIL30.yaml` e
   `.PROB.yaml`). No agente, passe `DB_PASSWORD` ao contêiner (`environment: - DB_PASSWORD=${DB_PASSWORD}`).

3. Execute um cenário inteiro, na ordem sorteada com a semente da tabela abaixo:

   ```
   ./rodar_cenario.sh 1      # validação, Cenário 1 (semente 2030, prefixo n1_)
   ./rodar_cenario.sh 2      # validação, Cenário 2 (semente 2031, prefixo n2_)
   ./rodar_cenario.sh 3      # validação, Cenário 3 (semente 2032, prefixo n3_)
   ./rodar_cenario.sh 4      # ajuste do decision_wait (D, Cenário 2, prefixo w2_)
   ./rodar_cenario.sh 5|6|7  # B x D pareado, Cenários 1, 2 e 3 (sementes 2033 a 2035)
   ```

   `-n` mostra o plano sem executar. Uma execução avulsa: `./rodar_medicao.sh NOME on|off [none|keystone|stress]`.

4. Resuma e analise (os comandos exatos também são impressos ao fim de cada rodada):

   ```
   python3 resumir_rodada.py --iface <interface> --skip 2 --conds BD --prefixo n6_ --prom $PROM --log medicao_log.csv
   python3 coletar_qa2.py --log medicao_log.csv --prefixo n6_ --conds BD --detalhe --loki http://<IP>:3100 --jaeger http://<IP>:16686
   python3 analisar_deteccao.py --log medicao_log.csv --prefixo n6_ n7_ --conds BD --prom $PROM --loki http://<IP>:3100 --jaeger http://<IP>:16686
   ```

## Limitações dos dados

- Os *traces* do Jaeger expiram em 72 horas, então as contagens de *traces* das rodadas antigas só podem ser
  conferidas pelos arquivos de saída em `dados/resumos/`, e não por nova consulta ao Jaeger.
- O Locust, o `sar` e a injeção das falhas rodaram no próprio nó, e o `sar` mede o *host* inteiro.
- Os números são de um único nó, com 5 execuções por condição. Veja as ameaças à validade no Capítulo 6.

## Segurança

Nenhuma senha, chave ou *token* está neste repositório. Os endereços do ambiente foram substituídos por
`IP_DO_SERVIDOR`.
