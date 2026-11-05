# FILE: Makefile
# RUOLO: Centro di automazione e interfaccia unificata per tutti i comandi dell'esame.

# .PHONY: Dichiara a Make che i nomi elencati NON sono file fisici su disco, ma "bersagli" (target) da eseguire sempre.
# Senza .PHONY, la presenza di una directory omonima ai target ne impedirebbe la regolare esecuzione.
.PHONY: help setup env env-cpu-minimal env-cpu-train env-cuda env-rocm env-auto train test build scan cluster-up deploy test-endpoint stress-test cluster-down logs clean

# VARIABILI:
# ?= significa "assegna questo valore se la variabile non e gia stata definita esternamente".
CLUSTER_NAME ?= scc-cluster
IMAGE_NAME ?= diabetes-api:latest

# Rilevamento automatico dell'eseguibile Python e Pytest nell'ambiente virtuale attivo (venv/)
# Se esiste venv/bin/python3 usa quello del virtualenv, altrimenti usa l'interprete di sistema.
PYTHON ?= $(shell if [ -f venv/bin/python3 ]; then echo "venv/bin/python3"; else echo "python3"; fi)
PYTEST ?= $(shell if [ -f venv/bin/pytest ]; then echo "venv/bin/pytest"; else echo "pytest"; fi)

# TARGET PREDEFINITO (eseguito digitando semplicemente 'make' senza argomenti):
# Mostra la guida interattiva con tutti i comandi disponibili.
help:
	@echo "Comandi disponibili nel progetto Secure Cloud Computing:"
	@echo "  make setup            - Installa i prerequisiti host su Linux o macOS"
	@echo "  make env              - Gestione ambienti virtuali (CPU, CUDA/ROCm su Linux)"
	@echo "  make env-cpu-minimal  - Crea/attiva venv minimale solo runtime & test (~100MB)"
	@echo "  make env-cpu-train    - Crea/attiva venv standard PyTorch CPU (~350MB) [CONSIGLIATO]"
	@echo "  make env-cuda         - Crea/attiva venv completo con supporto NVIDIA CUDA (~2.5GB)"
	@echo "  make env-rocm         - Crea/attiva venv completo con supporto AMD ROCm (~2.5GB)"
	@echo "  make env-auto         - Rileva automaticamente la GPU presente e aggancia il venv corretto"
	@echo "  make train            - Esegue l'addestramento offline e genera l'artefatto model.onnx"
	@echo "  make test             - Esegue la suite di test automatizzati con pytest"
	@echo "  make build            - Compila l'immagine Docker hardened (Passo C)"
	@echo "  make scan             - Esegue Docker Scout sulle vulnerabilita CVE (Passi D, E)"
	@echo "  make cluster-up       - Crea il cluster KinD (1 master + 2 workers) con Ingress (Passo F)"
	@echo "  make deploy           - Esegue il deployment completo idempotente sul cluster K8s (Passo H)"
	@echo "  make test-endpoint    - Invia richieste curl di prova (/health e /predict)"
	@echo "  make stress-test      - Esegue lo stress test wrk con validazione SLA automatica (Passo G)"
	@echo "  make logs             - Mostra i log aggregati dei pod applicativi"
	@echo "  make cluster-down     - Distrugge il cluster KinD liberando le risorse del computer"

# 1. SETUP DELL'HOST: Esecuzione dello script con privilegi di amministratore per configurare l'ambiente
# Seleziona l'installer in base al sistema: Debian/Linux usa APT e sudo,
# macOS usa Homebrew senza sudo. Lo script PowerShell copre invece Windows.
setup:
	@case "$$(uname -s)" in \
		Darwin) bash scripts/setup_host_macos.sh ;; \
		Linux) sudo bash scripts/setup_host.sh ;; \
		*) echo "[-] Sistema operativo non supportato da make setup: $$(uname -s)" >&2; exit 1 ;; \
	esac

# GESTIONE AMBIENTI VIRTUALI PYTHON (SUPPORTO HARDWARE ETEROGENEO):
# I target passano un profilo esplicito al gestore Bash; "env" mostra il menu,
# mentre "env-auto" sceglie in base all'hardware supportato dalla piattaforma.
env:
	./scripts/setup_env.sh

env-cpu-minimal:
	./scripts/setup_env.sh cpu-minimal

env-cpu-train:
	./scripts/setup_env.sh cpu-train

env-cuda:
	./scripts/setup_env.sh gpu-cuda

env-rocm:
	./scripts/setup_env.sh gpu-rocm

env-auto:
	./scripts/setup_env.sh auto

# 2. ADDESTRAMENTO OFFLINE: Esegue lo script Python per allenare la LSTM ed esportare model.onnx
train:
	$(PYTHON) model/train_mock_model.py

# 3. TEST AUTOMATIZZATI: Esegue pytest con flag -v (verbose: stampa il nome di ogni singolo test e l'esito)
test:
	$(PYTEST) tests/ -v

# 4. COMPILAZIONE DOCKER: Compilazione dell'immagine hardened multi-stage
build:
	docker build -t $(IMAGE_NAME) -f docker/Dockerfile .

# 5. AUDIT DI SICUREZZA (DOCKER SCOUT):
# - quickview: Fornisce un riassunto immediato del numero di vulnerabilita (Critical, High, Medium, Low).
# - cves: Elenca nel dettaglio ogni singolo pacchetto vulnerabile con il relativo identificativo CVE e la versione con la patch correttiva.
scan:
	docker scout quickview $(IMAGE_NAME)
	docker scout cves $(IMAGE_NAME)

# 6. CREAZIONE DEL CLUSTER KIND:
# Crea il cluster multi-nodo, installa il controller NGINX e applica la patch per le annotazioni di sicurezza.
# Richiede Docker attivo e i comandi kind/kubectl disponibili nel PATH.
cluster-up:
	kind create cluster --name $(CLUSTER_NAME) --config k8s/kind-config.yaml
	kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
	kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=180s
	kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p '{"data":{"allow-snippet-annotations":"true"}}' 2>/dev/null || true

# 7. DEPLOYMENT SUL CLUSTER:
# Invoca direttamente lo script unificato scripts/deploy_k8s.sh (Single Source of Truth)
deploy:
	./scripts/deploy_k8s.sh

# 8. TEST MANUALE CON CURL:
# Invia una chiamata GET a /health e una POST di esempio (dati sintetici) a /predict
test-endpoint:
	@echo "[+] Test Health Probe (/health):"
	curl -s -i http://localhost/health
	@echo "\n\n[+] Test Readiness Probe (/ready):"
	curl -s -i http://localhost/ready
	@echo "\n\n[+] Test Predict (/predict):"
	curl -s -i -X POST http://localhost/predict \
		-H 'Content-Type: application/json' \
		-d '{"patient_id": "PATIENT_DEMO", "features": [[110,0,0],[112,0,0],[115,0,0],[120,0,0],[130,50,0],[150,0,0],[170,0,0],[165,0,0],[155,0,0],[145,0,0],[135,0,0],[130,0,0]]}'
	@echo "\n"

# 9. STRESS TEST CON WRK:
# Esegue lo script di validazione SLA simulando 100 connessioni simultanee su 4 thread per 30 secondi
# Verificando automaticamente throughput (>=83.33 RPS), latenza p99 (<=1000ms) ed errori (<0.5%)
stress-test:
	./scripts/run_stress_test.sh http://localhost 30s 4 100

# 10. OSSERVABILITÀ DEI LOG:
# kubectl logs: Mostra i log generati dall'applicazione.
# - -l app=diabetes-forecasting-api: Aggrega i log di TUTTE e 3 le repliche contemporaneamente!
# - -f: Modalita 'follow' (rimane in ascolto stampando i nuovi log in tempo reale come 'tail -f').
# - --max-log-requests=10: Permette di visualizzare simultaneamente i flussi di log di piu container.
logs:
	kubectl logs -l app=diabetes-forecasting-api -f --max-log-requests=10

# 11. PULIZIA DEL CLUSTER:
# Elimina il cluster KinD e cancella tutti i container, nodi e volumi temporanei creati
cluster-down:
	kind delete cluster --name $(CLUSTER_NAME)
