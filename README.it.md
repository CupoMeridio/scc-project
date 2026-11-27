# Secure Cloud Computing - ML Serving SaaS su Kubernetes

**Lingua:** [English](README.md) | **Italiano**

[![Kubernetes](https://img.shields.io/badge/Kubernetes-KinD_v0.25-blue.svg)](https://kubernetes.io/)
[![NGINX](https://img.shields.io/badge/Gateway-NGINX_Ingress-green.svg)](https://kubernetes.github.io/ingress-nginx/)
[![Container Hardening](https://img.shields.io/badge/Security-Non--Root_UID_10001-blue.svg)](docker/Dockerfile)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI_%2B_ONNX-teal.svg)](https://fastapi.tiangolo.com/)
[![Cross-Platform](https://img.shields.io/badge/Platform-Linux_%7C_macOS_%7C_Windows-orange.svg)](README.it.md)

---

## 1. Panoramica del Progetto & Contesto Clinico

> **[Consulta la traccia ufficiale del progetto](docs/traccia.pdf)** per i requisiti accademici e i vincoli di consegna completi.

### Le Tre Opzioni della Traccia Ufficiale:
1. **Opzione 1:** Monitoraggio predittivo e allerta precoce per pazienti con Diabete di Tipo 1 (*Time Series Forecasting* & *Clinical Risk Classification*). *(Nota SaMD: come chiarito dal docente, il dosaggio farmacologico di insulina è responsabilità esclusiva del medico; il sistema SaaS agisce da Early Warning System predicendo la glicemia a 30 minuti $G(t+6)$ e classificando il rischio su 7 fasce cliniche sul dataset reale OhioT1DM).*
2. **Opzione 2:** Discriminazione tra soggetti sani e pazienti affetti da Parkinson (*Classificazione*).
3. **Opzione 3:** Discriminazione tra soggetti sani e pazienti affetti da Alzheimer (*Classificazione*).

> [!IMPORTANT]
> **Nota Architetturale: Approccio Modulare e Dominio di Riferimento Provvisorio**<br>
> Poiché i dataset definitivi non sono stati ancora rilasciati e l'assegnazione finale potrebbe essere libera o guidata, in questa **Fase di Costruzione dell'Infrastruttura** è stata adottata l'**Opzione 1 (Diabete di Tipo 1)** come **caso d'uso prototipale di riferimento**.
>
> **Motivazione dell'Opzione 1 per il Collaudo Iniziale:**
> - Impone vincoli architetturali più stringenti: la natura sequenziale dei dati (*Time Series*) e la criticità clinica consentono di testare la validazione temporale (13 passi di glucosio + 14 esogeni), il runtime ONNX e i **Safety Guardrails** fisiologici applicativi (Clamp [1.0, 35.0] mmol/L e 7 classi di rischio).
> - L'SLA dell'Opzione 1 (servizio attivo 7 giorni su 7, 6:00-22:00) è il più restrittivo e include pienamente le finestre operative delle Opzioni 2 e 3 (attive solo dal lunedì al venerdì, 8:00-18:00).
>
> **Garanzia di Portabilità Domain-Agnostic (Plug-and-Play):**<br>
> L'infrastruttura Cloud (cluster KinD multi-nodo, Dockerfile multi-stage non-root, Ingress NGINX con Rate Limiting, politiche di sicurezza Kubernetes, stress test `wrk` e script di deployment e validazione automatizzati pronti per l'integrazione CI/CD) è **completamente disaccoppiata dal tipo di modello**. 
> Qualora venga confermata l'Opzione 1 oppure assegnata l'Opzione 2 (Parkinson) o 3 (Alzheimer), l'**infrastruttura rimarrà identica al 100%**: risulterà sufficiente sostituire l'artefatto compilato `model/model.onnx` e aggiornare lo schema Pydantic per le nuove feature.

### Mappa dell'Architettura: Separazione a Zone e Minimo Privilegio (`attach/arch.pdf`)

```text
Browser / Utente (Paziente, Diabetologo, Center Manager)
        ↓ HTTPS obbligatorio (redirect 308 da HTTP)
Ingress NGINX (Gateway: TLS, Rate Limiting, Path-Based Routing)
   ├── "/"                     ──▶ Frontend (2 repliche NGINX non-root, HTML/CSS/JS + Chart.js locale)
   ├── "/api/auth/*"           ──▶ IAM Zone (`diabetes-iam-api`, 2 repliche, ruolo `saas_iam`, firma Ed25519)
   ├── "/api/patient|doctor/*" ──▶ Clinical Zone (`diabetes-clinical-api`, 2 repliche, ruolo `saas_clinical`)
   │                                  ├──▶ Verifica locale JWT EdDSA + Redis `revocation_ro` (TLS)
   │                                  └──▶ ML Inference (`diabetes-forecasting-service`, 3–6 repliche via HPA, HTTPS)
   └── "/api/manager/*"        ──▶ Manager Zone (`diabetes-manager-api`, 2 repliche, ruolo `saas_manager` su viste SQL)

Solo Interni (NON esposti da Ingress):
   ├── CGM Ingestion (`diabetes-ingest-service`, ruolo `saas_ingest`) ──▶ Coda Redis (`rediss://`) ──▶ Worker (`saas_worker`) ──┐
   ├── PostgreSQL HA (CNPG Primary + Standby, `verify-full` + `pg_dump` cifrato X25519/HKDF-SHA256/AES-256-GCM)              ◀┘
   └── Training Zone CronJob (`diabetes-model-trainer`, ruolo `saas_trainer` su `v_training_cgm_series`)
          └── Gate Champion/Challenger ──▶ Model Repository (`diabetes-model-repo`, PVC + SHA-256 + firma Ed25519)
                                              └──▶ Sync Fail-Closed + Rolling Restart su `diabetes-forecasting-api`
```

Il gateway **Ingress NGINX** espone verso l'esterno solo il portale statico (`/`) e le zone API separate (`/api/auth/*`, `/api/patient/*`, `/api/doctor/*`, `/api/manager/*`), forzando HTTPS (`ssl-redirect: "true"` + `force-ssl-redirect: "true"`, `backend-protocol: HTTPS`). Clinical e Manager verificano localmente i JWT **EdDSA (Ed25519)** e controllano la revoca (`jti`/`pwdv`) su Redis ACL in sola lettura (`revocation_ro` su `rediss://`); la zona Manager non ha alcun accesso di rete né credenziali verso IAM. Ogni zona è isolata da **NetworkPolicy `default-deny` + allowlist** per zona e usa un proprio ruolo PostgreSQL dedicato (`saas_iam`, `saas_clinical`, `saas_manager`, `saas_ingest`, `saas_worker`, `saas_trainer`): `saas_manager` legge solo viste aggregate (`v_manager_*`), e `saas_trainer` legge solo la vista pseudonimizzata (`v_training_cgm_series` con SHA-256 + salt). Il **Model Repository** distribuisce i pesi Champion ONNX su PVC con verifica SHA-256 + firma Ed25519 e sync **fail-closed** sulle repliche ML (3–6 pod scalati da Kubernetes `HorizontalPodAutoscaler` con `podAntiAffinity` e `PDB`). I backup giornalieri `pg_dump` usano cifratura asimmetrica autenticata (**X25519 + HKDF-SHA256 + AES-256-GCM**, con chiave privata custodita fuori cluster). Vedi [Mappa di corrispondenza architettura](docs/mappa_architettura.md).

---

## 2. Mappatura Task della Traccia d'Esame (Tasks A – H)

La traccia ufficiale dell'esame definisce 8 compiti sequenziali. La tabella seguente illustra la corrispondenza esatta tra ciascun task accademico, il comando automatizzato e i file implementativi:

| Task Traccia | Descrizione Formale del Requisito | Comando Linux/macOS (Make) | Comando Windows (PowerShell) | File Chiave |
| :--- | :--- | :--- | :--- | :--- |
| **Task A** | Addestramento offline del modello ML | `make train` | `.\venv\Scripts\python.exe model/train_lstm.py` | [`model/train_lstm.py`](model/train_lstm.py) |
| **Task B** | Salvataggio artefatto e costruzione Web App per inferenza | `make test` | `.\venv\Scripts\pytest.exe tests/ -v` | [`app/main.py`](app/main.py), [`app/schemas.py`](app/schemas.py) |
| **Task C** | Containerizzazione multi-stage non-root con Docker Desktop | `make build` | `docker build -t diabetes-api:1.0.0 -f docker/Dockerfile .` | [`docker/Dockerfile`](docker/Dockerfile), [`docker/Dockerfile.saas`](docker/Dockerfile.saas), [`docker/Dockerfile.frontend`](docker/Dockerfile.frontend) |
| **Task D & E** | Analisi vulnerabilità CVE e patching con Docker Scout | `make scan` | `docker scout cves diabetes-api:1.0.0` | [`docker/Dockerfile`](docker/Dockerfile) |
| **Task F** | Creazione cluster Kubernetes (1 Control-Plane + 2 Worker) | `make cluster-up` | `.\scripts\deploy_k8s.ps1` | [`k8s/kind-config.yaml`](k8s/kind-config.yaml) |
| **Task G** | Benchmark `wrk` e validazione automatica dei vincoli SLA su Linux/macOS | `make stress-test` | `.\scripts\run_stress_test.ps1` | [`scripts/run_stress_test.sh`](scripts/run_stress_test.sh), [`scripts/test_predict.lua`](scripts/test_predict.lua) |
| **Task H** | Deployment finale della Web App sul cluster Kubernetes | `make deploy` | `.\scripts\deploy_k8s.ps1` | [`k8s/deployment.yaml`](k8s/deployment.yaml), [`k8s/ingress.yaml`](k8s/ingress.yaml) |

---

## 3. Guida Passo-Passo per Nuovi Utenti (Quickstart Guide)

### Prerequisiti Minimi di Sistema
Assicurarsi che l'ambiente host soddisfi i seguenti requisiti tecnici prima dell'avvio:
* **Su Linux:** Shell Bash, `make`, `git`, `curl` e `sudo`. Lo script `make setup` usa APT e il repository Debian 13 (Trixie); su Ubuntu, Fedora e altre distribuzioni occorre installare i prerequisiti con il gestore di pacchetti appropriato.
* **Su macOS:** Homebrew, Bash, `make`, `git` e `curl`. `make setup` installa Docker Desktop, KinD, kubectl, `wrk`, `jq` e Python 3.
* **Su Windows (10/11):** **Docker Desktop** attivo con backend WSL2 abilitato e shell **PowerShell**.
* **Per gli ambienti Python locali:** Python **3.14** con supporto `venv` deve essere disponibile (`python3.14` su Linux/macOS, `py -3.14` su Windows). Le immagini Docker includono già il runtime Python 3.14.

---

### Approvvigionamento del Dataset (OhioT1DM — Decisione D1)

Il dataset clinico **non** è distribuito con questo repository e **non** è incluso in alcuna immagine container. Il repository GitHub è pubblico e OhioT1DM è rilasciato sotto un accordo d'uso che ne vieta la ridistribuzione.

* **Fonte:** il bundle ufficiale OhioT1DM `GlucoseClassification`, ottenuto sotto tale accordo (12 pazienti, 24 file CSV).
* **Dove collocarlo (in locale):** nella radice del repository, con questa struttura: `Diabetes/GlucoseClassification/<codice_paziente>/<codice_paziente>-ws-testing(t+30).csv` (e `-ws-training(t+30).csv`). Questo percorso viene letto direttamente da `make train` e `make simulate-cgm`.
* **Come raggiunge il Seed Job (nel cluster):** [`k8s/kind-config.yaml`](k8s/kind-config.yaml) dichiara `extraMounts`, che mappa la cartella locale `Diabetes/GlucoseClassification/` su `/mnt/ohiot1dm` in ogni nodo KinD. Il Job di seed demo ([`k8s/seed-demo-job.yaml`](k8s/seed-demo-job.yaml)) monta quel percorso in sola lettura e lo legge tramite la variabile d'ambiente `DATASET_DIR`. I volumi `hostPath` ignorano `fsGroup`: i file CSV devono essere leggibili da tutti (es. `chmod -R a+r Diabetes`).
* **Senza la cartella locale:** `make train` non può essere eseguito e `make seed-demo` crea comunque gli account demo ma registra un avviso e non carica alcuna serie storica CGM.

---

### Opzione A: Workflow Completo su Debian 13

#### Passo 1: Installazione degli strumenti di sistema (Docker, wrk, KinD, kubectl)
Esecuzione del setup per installare Docker CE, Docker Scout CLI, KinD, kubectl, wrk, python3-venv e associare l'utente al gruppo `docker`:
```bash
make setup
```
> [!IMPORTANT]
> Se Docker è stato appena installato sul sistema, applicare l'appartenenza al gruppo nella shell corrente senza effettuare il logout:
> ```bash
> newgrp docker
> ```

#### Passo 2: Configurazione dell'ambiente virtuale Python
Rilevamento dell'hardware grafico (AMD Radeon, NVIDIA CUDA o CPU pura) e predisposizione dell'ambiente in `.venvs/`:
```bash
make env-auto
```
*(In alternativa, è possibile selezionare direttamente il profilo standard CPU con `make env-cpu-train`)*.

#### Passo 3: Addestramento offline del modello (Task A)
Addestramento della rete neurale ed esportazione del file `model/model.onnx` con i metadati SHA256 in `model/model_metadata.json`:
```bash
make train
```

#### Passo 4: Esecuzione dei test automatizzati (Task B)
Verifica dei contratti dell'API, della forma del payload clinico (13 campioni di glucosio e 14 variabili esogene) e del rifiuto di valori non validi o NaN/Inf:
```bash
make test
```

#### Passo 5: Compilazione immagine Docker e Scansione di Sicurezza (Task C, D, E)
Compilazione dell'immagine multi-stage con utente non-root (UID 10001) e scansione CVE con Docker Scout:
```bash
make build
make scan
```

#### Passo 6: Deployment completo da zero (Task F + H)
Il comando compila le immagini, **elimina l'eventuale cluster KinD esistente e tutti i suoi dati** (database, PVC, Secret), quindi ricrea il cluster a 3 nodi, installa NGINX Ingress e distribuisce l'applicazione:
```bash
make deploy
```
`make cluster-up` resta disponibile per dimostrare separatamente il Task F; non va eseguito prima di `make deploy`, perché il deploy ricrea il cluster.

#### Passo 8: Verifica degli Endpoint e Portale Web HTTPS
Verifica diretta dell'isolamento Ingress, redirect HTTPS e salute SaaS da riga di comando:
```bash
make test-endpoint
```
Accesso al portale web HTTPS (con cookie HttpOnly e RBAC server-side per Paziente, Diabetologo e Center Manager):
* **Portale Web SaaS (Login & Dashboard):** [https://localhost](https://localhost)
* **Health / Readiness SaaS:** [https://localhost/api/health](https://localhost/api/health) / [https://localhost/api/ready](https://localhost/api/ready)
* **Seed Demo opzionale (Job dedicato):** `make seed-demo` — genera password demo casuali e le stampa una sola volta nel terminale. Il Job di seed le applica quando crea gli account demo (database vuoto). Nessuna credenziale demo è nel repository o viene applicata da `make deploy`.

#### Passo 8b: Simulazione Stream Sensore CGM dal Dataset Reale (OhioT1DM)
Per eseguire la simulazione in-process (senza cluster):
```bash
make simulate-cgm
```
`/api/cgm/ingest` è un endpoint **interno** (D2): non è raggiungibile tramite l'Ingress, quindi l'ingestione deve passare da un port-forward e richiede una chiave per dispositivo inviata nell'header `X-Device-Key`. La chiave è nel Secret `device-keys` creato dal Job di seed (una voce per paziente, decisione N1/Q3):
```bash
# In un terminale, apri il port-forward verso il servizio Ingest:
kubectl port-forward svc/diabetes-ingest-service 8080:80
# In un altro terminale, invia i campioni con la chiave del paziente:
make simulate-cgm-ingest PATIENT=591
```
Su Windows, `scripts/simulate_cgm_ingest.ps1` esegue lo stesso flusso (legge la chiave dal Secret, apre il port-forward ed esegue il simulatore).

#### Passo 9: Stress Test di Carico e Validazione SLA (Task G)
Esecuzione del benchmark automatizzato con `wrk` per convalidare i requisiti stringenti di Service Level Agreement stabiliti dalla traccia (Throughput $\ge 83.33\text{ RPS}$, Latenza $p99 \le 1000\text{ ms}$, Tasso di Errore $< 0.5\%$, corrispondente a una disponibilità del $99.5\%$):
```bash
make stress-test
```
Su Windows, `run_stress_test.ps1` esegue `wrk` all'interno di un container Docker ed espone le metriche aggregate di throughput e latenza.

#### Passo 10: Ispezione Log e Dismissione del Cluster
* Visualizzazione in tempo reale dei flussi di log aggregati delle 3 repliche:
  ```bash
  make logs
  ```
* Al termine dei collaudi, distruzione del cluster KinD per rilasciare integralmente la memoria RAM del sistema:
  ```bash
  make cluster-down
  ```

---

### Opzione B: Workflow su macOS (Docker Desktop + Homebrew)

#### Passo 1: Installazione degli strumenti
Con Homebrew installato, eseguire il provisioning di Docker Desktop, KinD, kubectl, `wrk` e `jq`:
```bash
make setup
```
Avviare Docker Desktop e attendere che il motore sia pronto:
```bash
open -a Docker
docker info
```

#### Passo 2: Ambiente Python e test
Su macOS il rilevamento automatico seleziona il profilo CPU; CUDA e ROCm non sono supportati:
```bash
make env-auto
make train
make test
```

#### Passo 3: Build e deployment
Usare gli stessi comandi Make del workflow Linux per build, deployment distruttivo da zero su KinD e stress test:
```bash
make build
make scan
make deploy
make test-endpoint
make stress-test
```

---

### Opzione C: Workflow su Windows (Docker Desktop + PowerShell)

#### Passo 1: Installazione strumenti (KinD e kubectl via winget)
Esecuzione in PowerShell dello script di provisioning:
```powershell
.\scripts\setup_host.ps1
```

#### Passo 2: Configurazione Ambiente Python
Predisposizione automatica del virtual environment:
```powershell
.\scripts\setup_env.ps1 auto
```

#### Passo 3: Compilazione e Audit Docker Scout
```powershell
docker build -t diabetes-api:1.0.0 -f docker/Dockerfile .
docker build -t diabetes-saas-api:1.0.0 -f docker/Dockerfile.saas .
docker build -t diabetes-frontend:1.0.0 -f docker/Dockerfile.frontend .
docker build -t diabetes-trainer:1.0.0 -f docker/Dockerfile.trainer .
docker scout quickview diabetes-api:1.0.0
docker scout cves diabetes-api:1.0.0
```

#### Passo 4: Deployment Automatico sul Cluster
**Attenzione:** lo script elimina il cluster KinD esistente e tutti i suoi dati prima di ricrearlo.
```powershell
.\scripts\deploy_k8s.ps1
```
*(Crea il cluster KinD, carica le immagini `:1.0.0`, installa NGINX Ingress v1.15.1, CloudNativePG HA, Redis, Seed Job Alembic, SaaS API, Worker e Frontend)*.

#### Passo 5: Test degli endpoint
```powershell
curl.exe -s -o NUL -w "%{http_code}\n" http://localhost/          # atteso 308
curl.exe -s -k -o NUL -w "%{http_code}\n" https://localhost/predict  # atteso 404 (solo interno)
curl.exe -s -k https://localhost/api/health
curl.exe -s -k https://localhost/api/ready
```
Accesso a [https://localhost](https://localhost) nel browser per il portale web autenticato.

#### Passo 6: Esecuzione Stress Test con `wrk` (tramite container Docker)
```powershell
.\scripts\run_stress_test.ps1
```
Di default (`-TargetHost internal`), lo script apre un `kubectl port-forward` verso il servizio ML interno, salva l'output grezzo in `docs/audit_reports/wrk_<data>.txt` e verifica programmaticamente throughput $\ge 83.33$ RPS, latenza p99 $\le 1000$ ms ed errori $\le 0.50\%$, uscendo con codice diverso da zero in caso di fallimento.

---

## 4. Service Level Agreement (SLA) & Dimensionamento

La traccia definisce finestre operative differenti a seconda del progetto scelto:
- **Uptime Operativo:**
  - *Opzione 1 (Diabete):* Dalle 6:00 alle 22:00, **7 giorni su 7**.
  - *Opzioni 2 e 3 (Parkinson / Alzheimer):* Dalle 8:00 alle 18:00, **Lunedì - Venerdì**.
- **Bacino Utenti:** Circa **50.000 pazienti / clienti registrati**.
- **Carico di Picco:** Fino a **5.000 richieste al minuto** (come definito dalla traccia; non significa 5.000 richieste simultanee).
- **Throughput Target:** $\frac{5000 \text{ req}}{60 \text{ s}} \approx \mathbf{83.33 \text{ RPS}}$ (Richieste al Secondo).
- **Tempo di Risposta (Latenza):** $\le \mathbf{1.0 \text{ secondo}}$ per singola risposta.
- **Disponibilità richiesta:** $\ge \mathbf{99.5\%}$ del tempo operativo, da valutare su un periodo adeguato.
- **Tolleranza Guasti (Outage):** Durata massima interruzione 30 minuti, tempo medio di ripristino (MTTR) $\le \mathbf{3 \text{ minuti}}$.

### Decisioni Ingegneristiche Chiave:
1. **FastAPI + Uvicorn + ONNX Runtime:**
   L'esportazione del grafo da PyTorch a formato standard ONNX consente l'esecuzione ad alte prestazioni su CPU tramite ONNX Runtime C++, riducendo sensibilmente l'overhead di memoria ed eliminando il peso del runtime di addestramento PyTorch in produzione, garantendo tempi di elaborazione ampiamente conformi all'SLA (< 1.0 s).
2. **Repliche e Resilienza Pod Kubernetes:**
   La distribuzione di 3 repliche pod su 2 nodi worker tramite `podAntiAffinity` (preferred) massimizza la ridondanza applicativa. L'adozione del PodDisruptionBudget (PDB con `minAvailable: 2`) e della strategia di `RollingUpdate` (`maxSurge: 1`, `maxUnavailable: 0`) garantisce l'alta disponibilità continua e l'aggiornamento a zero-downtime, rispettando l'obiettivo di uptime del 99.5%.
3. **NGINX Ingress come Perimetro di Sicurezza & Terminazione TLS:**
   Configurato come gateway perimetrale con Rate Limiting (1000 RPS con burst 5x per IP, dimensionato ~12 volte oltre l'obiettivo di 83.33 RPS per consentire lo stress test `wrk` da singolo IP senza falsi positivi 429), Connection Limiting (150 connessioni per IP contro Slowloris), terminazione TLS su porta 443 con certificato e chiave generati on-the-fly con SAN (`DNS:localhost, IP:127.0.0.1`), redirect HTTP→HTTPS obbligatorio (`ssl-redirect: "true"` e `force-ssl-redirect: "true"`) e nessuna annotazione snippet personalizzata (`allow-snippet-annotations=false`, D3). I verbi di scrittura non ammessi (`PUT`/`DELETE`) ricevono `405` da `frontend/nginx.conf` su `/` e dal routing FastAPI su `/api/*`.

---

## 5. Architettura di Sicurezza & Controlli Perimetrali

| Controllo di Sicurezza | Modello di Minaccia & Motivazione | Meccanismo di Implementazione |
| :--- | :--- | :--- |
| **Container Non-Root** | Previene Container Breakout ed escalation dei privilegi sul nodo host. | `USER 10001:10001` (`101` per NGINX unprivileged), `runAsNonRoot: true`, `readOnlyRootFilesystem: true`. |
| **Multi-Stage Build** | Riduce la superficie d'attacco; compilatori e strumenti di build sono esclusi dall'immagine finale. | Dockerfile a doppio stadio (`builder` -> `runner`), base image fissata per digest (`python:3.14.10-slim@sha256:22eafb37…`). |
| **Scansione CVE con Docker Scout** | Identifica Common Vulnerabilities and Exposures nel sistema operativo e nelle librerie Python prima del rilascio. | `docker scout cves` & `docker scout quickview` (salvati in `docs/audit_reports/`). |
| **Rate Limiting** | Previene attacchi Denial of Service (DoS) e garantisce la corretta ripartizione della capacità tra client concorrenti. | `nginx.ingress.kubernetes.io/limit-rps: "1000"`, `limit-burst-multiplier: "5"`. |
| **Connection Limiting** | Mitiga attacchi Slowloris e l'esaurimento della tabella dei socket/connessioni sul gateway. | `nginx.ingress.kubernetes.io/limit-connections: "150"`. |
| **Terminazione TLS / HTTPS** | Cifratura end-to-end dei dati biometrici in transito (HIPAA & GDPR Art. 32). | Secret `diabetes-tls-cert` su porta 443, `ssl-redirect: "true"`, `force-ssl-redirect: "true"`. |
| **Dimensione Massima Body HTTP** | Previene attacchi di esaurimento memoria (Memory Starvation) dato che il payload clinico pesa < 2 KB. | `nginx.ingress.kubernetes.io/proxy-body-size: "1m"`. |
| **Restrizione Metodi HTTP** | Principio del Minimo Privilegio: il portale statico accetta solo `GET`/`HEAD` (`405` altrimenti); le API SaaS instradano solo i verbi validi (`405` su `PUT`/`DELETE`). | Applicato nativamente in `frontend/nginx.conf` e nei router FastAPI (zero snippet Ingress). |
| **Header HTTP OWASP** | Difesa in profondità (Defense-in-Depth) per imporre standard moderni di sicurezza HTTP. | Iniettati da `frontend/nginx.conf` e middleware FastAPI (`X-Frame-Options`, `X-Content-Type-Options`, `CSP`, `HSTS`). |
| **Quote di Risorse K8s** | Previene attacchi DoS interni e monopolio delle risorse CPU/RAM sul nodo host (Noisy Neighbor). | Limiti container impostati su tutti i pod ML (`500m`/`256Mi`), SaaS, Worker, Redis e PostgreSQL HA. |

### 5.1 Validazione dei Dati e Ciclo di Vita del Modello
- **Contratto dell'Input:** Pydantic v2 garantisce l'integrità strutturale della serie temporale (13 misurazioni di glucosio in mmol/L e 14 variabili esogene), bloccando valori fuori range fisiologico e valori non numerici (`NaN`, `Inf`) con `HTTP 422 Unprocessable Entity` prima dell'esecuzione dell'algebra lineare.
- **Safety Clamp Fisiologico & Early Warning:** L'output è vincolato nel range vitale $[1.0, 35.0]\text{ mmol/L}$ e categorizzato su 7 classi di rischio per allertare il paziente in anticipo su ipo/iperglicemie.
- **Readiness Gate Fail-Closed:** `/ready` restituisce `HTTP 503 Service Unavailable` se la sessione ONNX o i parametri di normalizzazione dello scaler non sono caricati e validati in memoria, escludendo il pod dal routing prima di generare errori utente.
- **Supply Chain Security & Checksum SHA256:** Gli artefatti `model.onnx` e `model.onnx.data` sono verificati all'avvio confrontando l'impronta crittografica SHA256 con quella archiviata in `model/model_metadata.json`.
- **Perimetro Clinico — Non è un Dispositivo Medico, Nessuna Dose di Insulina:** Il servizio prevede la glicemia **a +30 minuti** in 7 classi di rischio come solo supporto decisionale di allerta precoce. **Non** calcola, consiglia né valida dosi di insulina o rapporti carboidrati; insulina attiva (IOB) e calcolo del bolo sono esplicitamente fuori perimetro e non devono mai essere derivati da questo output. È un artefatto prototipale, non un dispositivo medico certificato (nessuna marcatura CE/FDA), e non sostituisce il giudizio clinico.
- **Limiti del Modello (dichiarati, non nascosti):** La LSTM è addestrata sulla coorte **OhioT1DM** (piccola, monocentrica) con 13 passi di storia CGM e 14 variabili esogene. La precisione degrada con dati del sensore mancanti/radi, pasti non annunciati, attività fisica e pazienti fuori dalla distribuzione di addestramento. MAE e bias sono mostrati solo quando esiste almeno una coppia previsione/verità a terra; altrimenti la UI mostra «dati insufficienti» anziché un numero inventato.

### 5.2 Stato del Ciclo di Vita di Ingress NGINX e Uso del Dataset
- **Ingress NGINX mantenuto con mitigazioni (Decisione D3):** Il progetto fissa per digest `ingress-nginx/controller:v1.15.1`, ultima release della community prima dell'archiviazione di `kubernetes/ingress-nginx` (24 marzo 2026). Poiché per le vulnerabilità successive non arriveranno fix (es. CVE-2026-116472), le annotazioni snippet sono disabilitate (`allow-snippet-annotations=false`, mitigazione di CVE-2021-25742) e l'immagine del controller è pinnata per digest. La **Gateway API** è documentata come percorso di migrazione futuro; il layer Ingress è isolato dall'applicazione per poterlo sostituire senza toccare la logica di business.
- **Uso del dataset OhioT1DM (Decisione D1):** Il dataset clinico **non è ridistribuito** in questo repository pubblico. È utilizzato secondo l'accordo d'uso di OhioT1DM ed è montato in sola lettura nel Seed Job da un volume locale (`k8s/seed-demo-job.yaml`); né la storia Git né alcuna immagine container contengono i CSV.

---

## 6. Struttura del Repository e note sulle piattaforme

```
scc-project/
├── docs/
│   ├── traccia.pdf                       # Traccia ufficiale del progetto d'esame
│   ├── Analisi architettura funzionale/  # Relazione e diagrammi requisiti funzionali SaaS
│   └── Architettura cloud/               # Relazione e diagrammi architettura cloud
├── Diabetes/                # (solo locale, NON versionato) CSV OhioT1DM montati in sola lettura dal Seed Job (D1)
├── .dockerignore            # Esclusione file non necessari dal contesto di build
├── .gitattributes           # Normalizzazione terminatori di riga (LF per Linux, CRLF per PowerShell)
├── .gitignore               # Esclusione cache, venv e file temporanei
├── Makefile                 # Automazione unificata per Linux/macOS
├── requirements.txt         # Dipendenze minimali runtime ML (FastAPI, ONNX Runtime)
├── requirements.lock        # Lock con hash per l'immagine ML (pip-compile --generate-hashes)
├── requirements-saas.txt    # Dipendenze backend SaaS (FastAPI, SQLAlchemy, psycopg, PyJWT, bcrypt)
├── requirements-saas.lock   # Lock con hash per l'immagine SaaS/Worker/Seed
├── requirements-train.txt   # Dipendenze offline per l'addestramento (PyTorch, ONNX)
├── requirements-test.txt    # Dipendenze per la suite di test (pytest, httpx)
├── common/
│   └── __init__.py          # Costanti cliniche condivise (CLASS_BINS, classify_glucose)
├── app/                     # Microservizio interno di inferenza ONNX fail-closed (`MODEL_REPO_URL`)
├── saas_app/                # Servizi isolati per zona (`SERVICE_MODE=iam|clinical|manager|ingest`), `iam_client`, `model_repo`, Worker e seed Alembic
├── frontend/                # Portale HTML/CSS/JS classico + Chart.js locale + NGINX non-root
├── model/                   # Training LSTM offline, pipeline Champion/Challenger `retrain_pipeline.py`, metadati e ONNX
├── docker/                  # Dockerfile multi-stage non-root (`ML`, `SaaS/Repo/Worker/Seed`, `Frontend`, `Trainer`)
├── k8s/                     # Topologia KinD, CNPG HA, CronJob `pg_dump` cifrato, Redis, Model Repo PVC, Training CronJob, 4 Deployment SaaS isolati, HPA (3–6), PDB, NetworkPolicy per zona, Ingress
├── tests/                   # Test automatizzati (`test_api.py`, `test_saas_api.py`, `test_zone_architecture.py`, `test_simulate_cgm_ingest.py`)
└── scripts/                 # Provisioning, deploy KinD, simulatore CGM e benchmark wrk interno
```

### 6.1 Gestione Ambienti Virtuali per Hardware Eterogeneo (CPU, NVIDIA CUDA, AMD ROCm)

Per garantire massima flessibilità e portabilità tra postazioni di sviluppo e nodi di calcolo eterogenei (sistemi con GPU NVIDIA, GPU AMD Radeon o stazioni con sola CPU), il repository include un **gestore automatico degli ambienti virtuali** ([`scripts/setup_env.sh`](scripts/setup_env.sh) / [`scripts/setup_env.ps1`](scripts/setup_env.ps1)).

Su macOS è disponibile il profilo CPU; il rilevamento `auto` seleziona `cpu-train`, mentre i profili CUDA e ROCm sono rifiutati perché non supportati su questa piattaforma.

> [!NOTE]
> I binari NVIDIA CUDA e AMD ROCm non possono coesistere nel medesimo ambiente Python in quanto entrambi sovrascrivono i binding C++ di `torch`. Il gestore isola gli ambienti in directory dedicate sotto `.venvs/` e connette il profilo selezionato mediante il link simbolico `scc-project/venv`.

| Profilo | Dipendenze Installate | Dimensione | Scopo e Utilizzo |
| :--- | :--- | :--- | :--- |
| **`cpu-minimal`** | FastAPI, ONNX Runtime, Pydantic, uvicorn, onnx, pytest, httpx. **Zero PyTorch**. | **~100 MB** | Validazione rapida dell'API, build Docker, probe di liveness/readiness e cluster serving. |
| **`cpu-train`**<br>*(Consigliato)* | Tutto di `cpu-minimal` + PyTorch CPU, ONNX, NumPy e pandas. | Variabile | **Profilo di riferimento per l'esame**: addestramento CPU su Linux e macOS. |
| **`gpu-cuda`** | Tutto di `cpu-minimal` + **PyTorch con NVIDIA CUDA 13.0**. | Grande | Attivazione per ambienti dotati di GPU **NVIDIA** compatibili. |
| **`gpu-rocm`** | Su Linux: PyTorch con ROCm secondo quanto supportato dallo script. Su Windows: al momento installa PyTorch CPU, non ROCm. | Variabile | Usare l'accelerazione ROCm solo su piattaforme esplicitamente supportate; la rilevazione di una GPU AMD su Windows non attiva l'accelerazione. |
| **`auto`** | Rilevamento automatico GPU su Linux; seleziona `cpu-train` su macOS. | Dinamica | **Selezione del profilo adatta alla piattaforma**. |

**Vantaggio Architetturale:** Una volta predisposti gli ambienti, l'avvicendamento tra configurazioni hardware richiede **1 millisecondo** (aggiornamento del puntatore simbolico), senza necessità di scaricare nuovamente i pacchetti.

---

## 7. Risoluzione Problemi e Domande Frequenti (Troubleshooting & FAQ)

### D: Errore "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
* **Causa:** Il servizio di sistema di Docker non è attivo in background.
* **Soluzione (Linux):** Avviare il daemon Docker tramite:
  ```bash
  sudo systemctl start docker
  ```
* **Soluzione (Windows):** Avviare l'applicazione **Docker Desktop** e attendere lo stato attivo (icona verde stabile).
* **Soluzione (macOS):** Avviare Docker Desktop con `open -a Docker` e verificare la disponibilità del motore con `docker info`.

### D: Errore "permission denied while trying to connect to the Docker daemon" su Linux
* **Causa:** La sessione utente attiva non ha ancora caricato l'appartenenza al gruppo di sistema `docker`.
* **Soluzione:** Eseguire nel terminale:
  ```bash
  newgrp docker
  ```
  oppure effettuare una disconnessione e nuova autenticazione della sessione utente.

### D: Errore "failed to bind port 0.0.0.0:80: address already in use"
* **Causa:** Un servizio web locale preesistente sul sistema host (es. Apache o NGINX) occupa la porta 80.
* **Soluzione:** Individuare il processo attivo e procedere all'arresto del servizio:
  ```bash
  sudo ss -tulpn | grep :80
  sudo systemctl stop apache2 2>/dev/null || sudo systemctl stop nginx 2>/dev/null
  ```

### D: Come verificare lo stato di nodi, pod e ingress in Kubernetes?
I comandi standard di `kubectl` consentono l'ispezione dello stato operativo in qualsiasi momento:
```bash
kubectl get nodes -o wide      # Mostra i nodi del cluster (1 master + 2 worker) e gli IP interni
kubectl get pods -o wide       # Elenca le 3 repliche dell'API e il relativo nodo worker di esecuzione
kubectl get ingress            # Ispeziona le regole di routing dell'Ingress Controller NGINX
kubectl get services           # Mostra il servizio di bilanciamento interno ClusterIP
```
