"""
Cenários de carga para a Seção 4.5 do TCC (Primeira Rodada de Experimentos).

Bate diretamente nas APIs REST do Keystone, Nova, Neutron e Glance,
espelhando o que foi descrito na metodologia: autenticação, listagens e um
ciclo de provisionamento/remoção de instância. Toda requisição carrega o
cabeçalho de ativação do OSProfiler, para que o custo do rastreamento
distribuído esteja refletido nas métricas de recursos coletadas durante o
teste (ver Seção 4.5, passo 2).

Variáveis de ambiente necessárias:
  OS_PASSWORD          - senha do usuário admin do Keystone (obrigatória)
  OSPROFILER_HMAC_KEY  - osprofiler_secret do /etc/kolla/passwords.yml (obrigatória)
  GLANCE_IMAGE_ID       - UUID de uma imagem existente (ex.: cirros)
  NOVA_FLAVOR_ID         - UUID de um flavor existente (ex.: m1.tiny)
  NEUTRON_NETWORK_ID    - UUID de uma rede existente

Variáveis opcionais (têm valor padrão):
  OS_AUTH_URL, OS_USERNAME, OS_PROJECT_NAME, OS_USER_DOMAIN_NAME,
  OS_PROJECT_DOMAIN_NAME, NOVA_URL, NEUTRON_URL, GLANCE_URL

Exemplo de execução (rodada padrão):
  locust -f locustfile.py --host=http://192.168.0.250 \\
    --users 50 --spawn-rate 5 --run-time 10m --headless \\
    --csv=resultado_padrao --html=relatorio_padrao.html
"""

import os
import random
import time

from locust import HttpUser, task, between

# Interruptor do rastreamento (OSProfiler). Com TRACING=off, nenhuma requisição
# leva o cabeçalho de ativação. Serve para medir a execução "sem observabilidade"
# (custo da telemetria sobre o nó). Padrão: on.
TRACING = os.environ.get("TRACING", "on").strip().lower() not in ("off", "0", "false", "no")
print(f"[locustfile] TRACING={'on' if TRACING else 'off'}")

if TRACING:
    from osprofiler import profiler
    from osprofiler import web as osprofiler_web

# --- Configuração via variáveis de ambiente ---------------------------------

OS_AUTH_URL = os.environ.get("OS_AUTH_URL", "http://192.168.0.250:5000")
if not OS_AUTH_URL.rstrip("/").endswith("/v3"):
    OS_AUTH_URL = OS_AUTH_URL.rstrip("/") + "/v3"
OS_USERNAME = os.environ.get("OS_USERNAME", "admin")
OS_PASSWORD = os.environ["OS_PASSWORD"]
OS_PROJECT_NAME = os.environ.get("OS_PROJECT_NAME", "admin")
OS_USER_DOMAIN_NAME = os.environ.get("OS_USER_DOMAIN_NAME", "Default")
OS_PROJECT_DOMAIN_NAME = os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")

OSPROFILER_HMAC_KEY = os.environ["OSPROFILER_HMAC_KEY"] if TRACING else None

NOVA_URL = os.environ.get("NOVA_URL", "http://192.168.0.250:8774/v2.1")
NEUTRON_URL = os.environ.get("NEUTRON_URL", "http://192.168.0.250:9696/v2.0")
GLANCE_URL = os.environ.get("GLANCE_URL", "http://192.168.0.250:9292/v2")

GLANCE_IMAGE_ID = os.environ.get("GLANCE_IMAGE_ID")
NOVA_FLAVOR_ID = os.environ.get("NOVA_FLAVOR_ID")
NEUTRON_NETWORK_ID = os.environ.get("NEUTRON_NETWORK_ID")


def trace_headers():
    """Cabeçalhos de ativação do OSProfiler, ou vazio se TRACING=off.

    profiler.init() só cria uma instância nova se não houver nenhuma ativa;
    por isso profiler.clean() vem antes, garantindo um trace_id novo por
    requisição.
    """
    if not TRACING:
        return {}
    profiler.clean()
    profiler.init(OSPROFILER_HMAC_KEY)
    return osprofiler_web.get_trace_id_headers()


class OpenStackUser(HttpUser):
    """Simula um usuário/operador interagindo com as APIs do OpenStack."""

    wait_time = between(1, 3)

    def on_start(self):
        self.token = None
        self.project_id = None
        self._authenticate()

    # --- Autenticação --------------------------------------------------

    def _authenticate(self):
        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": OS_USERNAME,
                            "domain": {"name": OS_USER_DOMAIN_NAME},
                            "password": OS_PASSWORD,
                        }
                    },
                },
                "scope": {
                    "project": {
                        "name": OS_PROJECT_NAME,
                        "domain": {"name": OS_PROJECT_DOMAIN_NAME},
                    }
                },
            }
        }
        with self.client.post(
            f"{OS_AUTH_URL}/auth/tokens",
            json=body,
            headers=trace_headers(),
            name="Keystone - Auth (token issuance)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                self.token = resp.headers["X-Subject-Token"]
                self.project_id = resp.json()["token"]["project"]["id"]
                resp.success()
            else:
                resp.failure(f"Auth failed: {resp.status_code}")

    def _headers(self):
        headers = {"X-Auth-Token": self.token, "Content-Type": "application/json"}
        headers.update(trace_headers())
        return headers

    # --- Tarefas ---------------------------------------------------------

    @task(5)
    def keystone_reauth(self):
        """Reautentica periodicamente (emissão de novo token)."""
        self._authenticate()

    @task(10)
    def nova_list_servers(self):
        self.client.get(
            f"{NOVA_URL}/servers",
            headers=self._headers(),
            name="Nova - List Servers",
        )

    @task(2)
    def nova_provision_cycle(self):
        """Cria e remove uma instância mínima — mede o tempo de provisionamento
        (indicador de latência de operação crítica, ver Seção 4.4)."""
        if not (GLANCE_IMAGE_ID and NOVA_FLAVOR_ID and NEUTRON_NETWORK_ID):
            return  # variáveis não configuradas: pula esse cenário

        payload = {
            "server": {
                "name": f"locust-test-{random.randint(1, 100000)}",
                "imageRef": GLANCE_IMAGE_ID,
                "flavorRef": NOVA_FLAVOR_ID,
                "networks": [{"uuid": NEUTRON_NETWORK_ID}],
            }
        }
        server_id = None
        with self.client.post(
            f"{NOVA_URL}/servers",
            json=payload,
            headers=self._headers(),
            name="Nova - Create Server (provisionamento)",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 202):
                resp.failure(f"Create failed: {resp.status_code}")
            else:
                resp.success()
                server_id = resp.json()["server"]["id"]

        if server_id:
            time.sleep(6)  # tempo maior para o provisionamento terminar antes de remover
            self.client.delete(
                f"{NOVA_URL}/servers/{server_id}",
                headers=self._headers(),
                name="Nova - Delete Server",
            )

    @task(8)
    def neutron_list_networks(self):
        self.client.get(
            f"{NEUTRON_URL}/networks",
            headers=self._headers(),
            name="Neutron - List Networks",
        )

    @task(5)
    def neutron_list_ports(self):
        self.client.get(
            f"{NEUTRON_URL}/ports",
            headers=self._headers(),
            name="Neutron - List Ports",
        )

    @task(6)
    def glance_list_images(self):
        self.client.get(
            f"{GLANCE_URL}/images",
            headers=self._headers(),
            name="Glance - List Images",
        )
