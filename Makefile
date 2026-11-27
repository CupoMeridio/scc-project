# FILE: Makefile
# RUOLO: Centro di automazione e interfaccia unificata per tutti i comandi dell'esame.

# .PHONY: Dichiara a Make che i nomi elencati NON sono file fisici su disco, ma "bersagli" (target) da eseguire sempre.
# Senza .PHONY, la presenza di una directory omonima ai target ne impedirebbe la regolare esecuzione.
.PHONY: help setup env env-cpu-minimal env-cpu-train env-cuda env-rocm env-auto train retrain test test-pg build scan cluster-up cluster-start cluster-stop deploy verify-netpol verify-db-roles verify-backup seed-demo test-endpoint simulate-cgm simulate-cgm-ingest stress-test cluster-down logs clean

# VARIABILI:
# ?= significa "assegna questo valore se la variabile non e gia stata definita esternamente".
CLUSTER_NAME ?= scc-cluster
IMAGE_TAG ?= 1.0.0
IMAGE_NAME ?= diabetes-api:$(IMAGE_TAG)
SAAS_IMAGE_NAME ?= diabetes-saas-api:$(IMAGE_TAG)
FRONTEND_IMAGE_NAME ?= diabetes-frontend:$(IMAGE_TAG)
TRAINER_IMAGE_NAME ?= diabetes-trainer:$(IMAGE_TAG)

# Paziente OhioT1DM usato dal simulatore e dai target di ingestione.
PATIENT ?= 591

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
	@echo "  make env-cpu-train    - Crea/attiva venv PyTorch CPU [CONSIGLIATO]"
	@echo "  make env-cuda         - Crea/attiva venv PyTorch con supporto NVIDIA CUDA 13.0"
	@echo "  make env-rocm         - Crea/attiva venv PyTorch con supporto AMD ROCm 7.1"
	@echo "  make env-auto         - Rileva automaticamente la GPU presente e aggancia il venv corretto"
	@echo "  make train            - Esegue l'addestramento offline e genera l'artefatto model.onnx"
	@echo "  make retrain          - Avvia il Job Training Zone Champion/Challenger su Kubernetes"
	@echo "  make test             - Esegue la suite di test automatizzati con pytest"
	@echo "  make test-pg          - Esegue i test di sicurezza PostgreSQL 16 reali (ruoli, viste, trigger)"
	@echo "  make build            - Compila le immagini Docker multi-stage non-root (Passo C)"
	@echo "  make scan             - Esegue Docker Scout sulle vulnerabilita CVE (Passi D, E)"
	@echo "  make cluster-up       - Crea il cluster KinD (1 master + 2 workers) con Ingress (Passo F)"
	@echo "  make cluster-start    - Avvia manualmente i nodi KinD precedentemente fermati"
	@echo "  make cluster-stop     - Ferma i nodi KinD senza distruggere dati/stato (0 CPU/RAM)"
	@echo "  make deploy           - Elimina e ricrea il cluster KinD, poi esegue il deployment completo"
	@echo "  make verify-netpol    - Verifica assertiva dell'enforcement delle NetworkPolicy (P4)"
	@echo "  make verify-db-roles  - Verifica assertiva sul cluster dei 6 ruoli PostgreSQL e viste"
	@echo "  make verify-backup    - Verifica assertiva di backup cifrato X25519+AES-GCM e restore"
	@echo "  make seed-demo        - (OPT-IN) Carica dati demo con password casuali e le stampa a schermo"
	@echo "  make test-endpoint    - Invia richieste curl di prova (/health e /predict)"
	@echo "  make simulate-cgm     - Simula lo streaming continuo del sensore CGM dal dataset"
	@echo "  make simulate-cgm-ingest - Ingestione CGM dal cluster (port-forward + chiave dispositivo)"
	@echo "  make stress-test      - Esegue lo stress test wrk con validazione SLA automatica (Passo G)"
	@echo "  make logs             - Mostra i log aggregati dei pod applicativi"
	@echo "  make clean            - Rimuove cache Python, file temporanei e log di test"
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
	$(PYTHON) model/train_lstm.py

# 3. TEST AUTOMATIZZATI: Esegue pytest con flag -v (verbose: stampa il nome di ogni singolo test e l'esito)
test:
	$(PYTEST) tests/ -v

# 3b. TEST INTEGRAZIONE POSTGRESQL 16 REALE (V5):
# Avvia un container PostgreSQL 16 effimero, applica Alembic e verifica ruoli, privilegi, viste e trigger.
test-pg:
	@bash -euo pipefail -c '\
	  CID="scc-pg-test-$$$$"; \
	  PW="pgtest_$$(openssl rand -hex 8)"; \
	  trap "docker rm -f $$CID >/dev/null 2>&1 || true" EXIT; \
	  docker run -d --name "$$CID" -p 127.0.0.1:55432:5432 \
	    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$$PW" -e POSTGRES_DB=diabetes_saas_test \
	    postgres:16-alpine >/dev/null; \
	  for i in $$(seq 1 40); do \
	    if docker exec "$$CID" pg_isready -h 127.0.0.1 -p 5432 -U postgres -d diabetes_saas_test >/dev/null 2>&1; then sleep 1; break; fi; \
	    sleep 1; \
	  done; \
	  PYTHONPATH=. PG_ADMIN_URL="postgresql+psycopg://postgres:$$PW@127.0.0.1:55432/diabetes_saas_test" \
	    $(PYTEST) tests/pg/ -v'

# 4. COMPILAZIONE DOCKER: Compilazione delle immagini multi-stage non-root (UID 10001 / 101).
# Una sola immagine (Dockerfile.saas) serve IAM, Clinical, Manager, Ingest, Worker, ModelRepo e Seed:
# ciascuno differisce solo per SERVICE_MODE o comando di avvio nei manifest (P7).
# Dockerfile.trainer isola PyTorch CPU + ONNX nella sola Training Zone.
build:
	docker build -t $(IMAGE_NAME) -f docker/Dockerfile .
	docker build -t $(SAAS_IMAGE_NAME) -f docker/Dockerfile.saas .
	docker build -t $(FRONTEND_IMAGE_NAME) -f docker/Dockerfile.frontend .
	docker build -t $(TRAINER_IMAGE_NAME) -f docker/Dockerfile.trainer .

# 5. AUDIT DI SICUREZZA (DOCKER SCOUT — Decisione D8):
# Salva l'output grezzo in docs/audit_reports/scout_<data>.txt con comando, data e versione immagini.
scan:
	@bash -euo pipefail -c '\
	  mkdir -p docs/audit_reports; \
	  REPORT="docs/audit_reports/scout_$$(date -u +%Y%m%d-%H%M%SZ).txt"; \
	  { \
	    echo "=== Docker Scout CVE Report (D8) ==="; \
	    echo "Date (UTC): $$(date -u -Is)"; \
	    echo "Images: $(IMAGE_NAME) $(SAAS_IMAGE_NAME) $(FRONTEND_IMAGE_NAME) $(TRAINER_IMAGE_NAME)"; \
	    for img in $(IMAGE_NAME) $(SAAS_IMAGE_NAME) $(FRONTEND_IMAGE_NAME) $(TRAINER_IMAGE_NAME); do \
	      echo ""; \
	      echo "--- docker image inspect $$img (ID / Created) ---"; \
	      docker image inspect "$$img" --format "{{.Id}} {{.Created}}"; \
	      echo "--- docker scout quickview $$img ---"; \
	      docker scout quickview "$$img"; \
	      echo "--- docker scout cves $$img ---"; \
	      docker scout cves "$$img"; \
	    done; \
	  } 2>&1 | tee "$$REPORT"; \
	  echo "[✓] Output grezzo Docker Scout salvato in $$REPORT"'

# 6. CREAZIONE DEL CLUSTER KIND:
# Crea il cluster multi-nodo e installa il controller NGINX dal manifest vendorizzato (D3).
# Non scarica nulla da Internet a runtime: usa k8s/vendor/ingress-nginx-controller-v1.15.1.yaml
# con il controller pinnato per digest. allow-snippet-annotations=false e' un requisito di
# sicurezza (CVE-2021-25742), non opzionale: il target fallisce se non viene applicato.
# Richiede Docker attivo e i comandi kind/kubectl disponibili nel PATH.
cluster-up:
	kind create cluster --name $(CLUSTER_NAME) --config k8s/kind-config.yaml
	docker update --restart=no $$(kind get nodes --name $(CLUSTER_NAME)) >/dev/null
	sha256sum -c k8s/vendor/ingress-nginx-controller-v1.15.1.yaml.sha256
	@TMP_ING=$$(mktemp); \
	trap "rm -f $$TMP_ING" EXIT; \
	patch -s -o "$$TMP_ING" k8s/vendor/ingress-nginx-controller-v1.15.1.yaml < k8s/vendor/kind-ingress-ready.patch; \
	kubectl apply -f "$$TMP_ING"
	kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=180s
	kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p '{"data":{"allow-snippet-annotations":"false","server-tokens":"false"}}'
	@SNIPPETS=$$(kubectl get cm ingress-nginx-controller -n ingress-nginx -o jsonpath='{.data.allow-snippet-annotations}'); \
	if [ "$$SNIPPETS" != "false" ]; then \
	  echo "[-] allow-snippet-annotations atteso 'false', trovato '$${SNIPPETS:-vuoto}'." >&2; \
	  exit 1; \
	fi; \
	echo "[✓] allow-snippet-annotations=false confermato sulla ConfigMap del controller."

# 7. DEPLOYMENT DISTRUTTIVO DA ZERO:
# deploy_k8s.sh elimina il cluster KinD esistente, se presente, e lo ricrea senza preservare dati o credenziali.
deploy:
	CLUSTER_NAME=$(CLUSTER_NAME) IMAGE_TAG=$(IMAGE_TAG) ./scripts/deploy_k8s.sh

# 7b. VERIFICA ENFORCEMENT NETWORKPOLICY (P4):
# Esegue lo script assertivo: fallisce (exit 1) se le policy non sono applicate dal CNI.
# L'output grezzo viene salvato in docs/audit_reports/netpol_<data>.txt (D8).
verify-netpol:
	./scripts/verify_netpol.sh

verify-db-roles:
	./scripts/verify_db_roles.sh

verify-backup:
	./scripts/verify_backup.sh

# Seed demo OPT-IN: genera password casuali e carica i dati con quelle password.
# Per D4 il flusso è destroy/recreate: il DB è vuoto, quindi il seed (idempotente)
# inserisce gli account demo con le password generate. Le credenziali non sono mai
# presenti nel repo né nello script di deploy.
seed-demo:
	@set -e; \
	DEMO_PATIENT_PASSWORD=$$(openssl rand -hex 12); \
	DEMO_DOCTOR_PASSWORD=$$(openssl rand -hex 12); \
	DEMO_MANAGER_PASSWORD=$$(openssl rand -hex 12); \
	kubectl delete secret demo-credentials --ignore-not-found >/dev/null 2>&1 || true; \
	kubectl create secret generic demo-credentials \
	  --from-literal=DEMO_PATIENT_PASSWORD="$$DEMO_PATIENT_PASSWORD" \
	  --from-literal=DEMO_DOCTOR_PASSWORD="$$DEMO_DOCTOR_PASSWORD" \
	  --from-literal=DEMO_MANAGER_PASSWORD="$$DEMO_MANAGER_PASSWORD" >/dev/null; \
	kubectl delete job diabetes-saas-demo --ignore-not-found >/dev/null 2>&1 || true; \
	sed 's/:1\.0\.0/:$(IMAGE_TAG)/g' k8s/seed-demo-job.yaml | kubectl apply -f - >/dev/null; \
	kubectl wait --for=condition=complete job/diabetes-saas-demo --timeout=180s >/dev/null; \
	DEVICE_ARGS=$$(kubectl logs job/diabetes-saas-demo 2>/dev/null | sed -n 's/.*DEVICE_KEY \([^ ]*\) \([^ ]*\).*/--from-literal=\1=\2/p'); \
	if [ -n "$$DEVICE_ARGS" ]; then \
	  kubectl delete secret device-keys --ignore-not-found >/dev/null 2>&1 || true; \
	  kubectl create secret generic device-keys $$DEVICE_ARGS >/dev/null; \
	  echo "[✓] Secret device-keys creato con $$(echo "$$DEVICE_ARGS" | wc -l) chiavi dispositivo (N1)."; \
	else \
	  echo "[!] Nessuna chiave dispositivo nei log del Job (DB già popolato?):"; \
	  echo "    le chiavi esistenti non sono recuperabili (in DB resta solo l'hash SHA-256)."; \
	  echo "    Rigenerane una con: kubectl exec deployment/diabetes-clinical-api -- python3 -m saas_app.device_cli create --patient <CODICE>"; \
	fi; \
	echo "[✓] Credenziali demo appena generate (non sono nel repository):"; \
	echo "  pazienti: $$DEMO_PATIENT_PASSWORD"; \
	echo "  medici:   $$DEMO_DOCTOR_PASSWORD"; \
	echo "  manager:  $$DEMO_MANAGER_PASSWORD"

# 8. TEST MANUALE CON CURL:
# Verifica il portale pubblico HTTPS (Frontend + SaaS API) e l'isolamento dell'Ingress (no /predict pubblico)
test-endpoint:
	@echo "[+] Verifica redirect HTTP -> HTTPS (308):"
	curl -s -o /dev/null -w "HTTP / -> %{http_code}\n" http://localhost/
	@echo "[+] Verifica che /predict e /metrics NON siano esposti pubblicamente sull'Ingress (atteso 404):"
	curl -s -k -o /dev/null -w "HTTPS /predict -> %{http_code}\n" https://localhost/predict
	curl -s -k -o /dev/null -w "HTTPS /metrics -> %{http_code}\n" https://localhost/metrics
	@echo "[+] Test SaaS Health & Ready via HTTPS:"
	curl -s -k -i https://localhost/api/health
	@echo "\n"
	curl -s -k -i https://localhost/api/ready
	@echo "\n"

# 9. SIMULAZIONE SENSORE CGM (Continuous Glucose Monitor):
# Esegue lo script di replay temporale dal dataset OhioT1DM in modalità in-process (--local)
simulate-cgm:
	$(PYTHON) scripts/simulate_cgm_patient.py --local --patient $(PATIENT) --steps 15 --delay 0.3

# 9b. INGESTIONE CGM VERSO IL CLUSTER (endpoint interno: serve il port-forward, D2):
# Legge la chiave del dispositivo dal Secret `device-keys` creato dal Job di seed (N1),
# apre il port-forward verso il servizio SaaS e invia i campioni a /api/cgm/ingest.
simulate-cgm-ingest:
	@KEY=$$(kubectl get secret device-keys -o jsonpath='{.data.patient_$(PATIENT)}' 2>/dev/null | base64 -d); \
	if [ -z "$$KEY" ]; then \
	  echo "[-] Chiave del paziente $(PATIENT) assente nel Secret device-keys."; \
	  echo "    Esegui prima 'make seed-demo' e verifica con: kubectl get secret device-keys"; \
	  exit 1; \
	fi; \
	echo "[+] Port-forward verso svc/diabetes-ingest-service 8080:443 (TLS interno Q2)..."; \
	kubectl port-forward svc/diabetes-ingest-service 8080:443 >/dev/null 2>&1 & \
	PF_PID=$$!; \
	trap "kill $$PF_PID 2>/dev/null" EXIT; \
	sleep 2; \
	$(PYTHON) scripts/simulate_cgm_patient.py --ingest --endpoint https://127.0.0.1:8080/predict --insecure \
	  --patient $(PATIENT) --steps 10 --delay 0.3 --device-key "$$KEY"

# 10. STRESS TEST CON WRK:
# Esegue lo stress test contro il servizio ML interno (non esposto pubblicamente)
stress-test:
	./scripts/run_stress_test.sh internal 30s 4 100

# 10b. RETRAINING IN-CLUSTER (Training Zone Champion/Challenger):
retrain:
	@bash -euo pipefail -c '\
	  mkdir -p docs/audit_reports; \
	  REPORT="docs/audit_reports/retrain_$$(date -u +%Y%m%d-%H%M%SZ).txt"; \
	  kubectl delete job diabetes-model-trainer-manual --ignore-not-found >/dev/null; \
	  kubectl create job --from=cronjob/diabetes-model-trainer diabetes-model-trainer-manual >/dev/null; \
	  kubectl wait --for=condition=complete job/diabetes-model-trainer-manual --timeout=600s; \
	  { \
	    echo "=== Model Retrain & Champion/Challenger Report (V3/V4) ==="; \
	    echo "Date (UTC): $$(date -u -Is)"; \
	    echo "Command: kubectl create job --from=cronjob/diabetes-model-trainer diabetes-model-trainer-manual"; \
	    echo "--- ML Deployment After Job ---"; \
	    kubectl get deployment diabetes-forecasting-api -o wide; \
	    echo "--- Trainer Job Logs ---"; \
	    kubectl logs job/diabetes-model-trainer-manual; \
	  } | tee "$$REPORT"; \
	  echo "[✓] Report retraining salvato in $$REPORT"'

# 11. OSSERVABILITÀ DEI LOG:
logs:
	kubectl logs -l 'app in (diabetes-forecasting-api,diabetes-iam-api,diabetes-clinical-api,diabetes-manager-api,diabetes-ingest-api,diabetes-ingestion-worker,diabetes-model-repo,diabetes-frontend)' -f --max-log-requests=20

# 11. AVVIO/ARRESTO/PULIZIA DEL CLUSTER:
# Avvia manualmente i container nodo KinD esistenti (nessun avvio automatico al boot del PC).
cluster-start:
	@bash -euo pipefail -c '\
	  NODES=$$(kind get nodes --name $(CLUSTER_NAME) 2>/dev/null); \
	  if [ -z "$$NODES" ]; then echo "[-] Nessun cluster '\''$(CLUSTER_NAME)'\'' trovato. Esegui '\''make deploy'\''." >&2; exit 1; fi; \
	  docker update --restart=no $$NODES >/dev/null; \
	  docker start $$NODES >/dev/null; \
	  for _i in $$(seq 1 30); do \
	    if kubectl get --raw=/readyz >/dev/null 2>&1 && kubectl auth can-i get clusters.postgresql.cnpg.io >/dev/null 2>&1; then break; fi; \
	    sleep 2; \
	  done; \
	  kubectl wait --for=condition=Ready nodes --all --timeout=120s; \
	  kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=120s; \
	  kubectl wait --for=condition=Ready cluster/diabetes-pg --timeout=180s; \
	  kubectl wait --for=condition=Available deployment/diabetes-redis deployment/diabetes-model-repo deployment/diabetes-forecasting-api deployment/diabetes-iam-api deployment/diabetes-clinical-api deployment/diabetes-manager-api deployment/diabetes-ingest-api deployment/diabetes-ingestion-worker deployment/diabetes-frontend --timeout=180s; \
	  echo "[✓] Cluster '\''$(CLUSTER_NAME)'\'' avviato e pronto (tutti i servizi Ready)."'

# Ferma i container nodo KinD senza eliminare dati, PVC o segreti (disabilita restart automatico al boot).
cluster-stop:
	@NODES=$$(kind get nodes --name $(CLUSTER_NAME) 2>/dev/null); \
	if [ -z "$$NODES" ]; then echo "[!] Nessun nodo trovato per '$(CLUSTER_NAME)'."; exit 0; fi; \
	docker update --restart=no $$NODES >/dev/null; \
	docker stop $$NODES >/dev/null; \
	echo "[✓] Cluster '$(CLUSTER_NAME)' fermato (0 CPU/RAM; riavviabile con 'make cluster-start')."

# Elimina il cluster KinD e cancella tutti i container, nodi e volumi temporanei creati
cluster-down:
	kind delete cluster --name $(CLUSTER_NAME)

# 12. PULIZIA ARTEFATTI TEMPORANEI:
# Rimuove cache bytecode Python, directory temporanee pytest e file effimeri di test
clean:
	@echo "[+] Pulizia cache Python e file temporanei..."
	rm -rf .pytest_cache __pycache__ app/__pycache__ tests/__pycache__ model/__pycache__
