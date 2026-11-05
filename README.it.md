# Secure Cloud Computing - ML Serving SaaS su Kubernetes

**Lingua:** [English](README.md) | **Italiano**

[![Kubernetes](https://img.shields.io/badge/Kubernetes-KinD_v0.25-blue.svg)](https://kubernetes.io/)
[![NGINX](https://img.shields.io/badge/Gateway-NGINX_Ingress-green.svg)](https://kubernetes.github.io/ingress-nginx/)
[![Docker Scout](https://img.shields.io/badge/Security-Docker_Scout_Hardened-brightgreen.svg)](https://docs.docker.com/scout/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI_%2B_ONNX-teal.svg)](https://fastapi.tiangolo.com/)
[![Cross-Platform](https://img.shields.io/badge/Platform-Linux_%7C_macOS_%7C_Windows-orange.svg)](README.it.md)

---

## 1. Panoramica del Progetto & Contesto Clinico

> **[Consulta la traccia ufficiale del progetto](traccia.pdf)** per i requisiti accademici e i vincoli di consegna completi.

### Le Tre Opzioni della Traccia Ufficiale:
1. **Opzione 1:** Previsione dell'assunzione di insulina per pazienti affetti da Diabete di Tipo 1 (*Time Series Forecasting*).
2. **Opzione 2:** Discriminazione tra soggetti sani e pazienti affetti da Parkinson (*Classificazione*).
3. **Opzione 3:** Discriminazione tra soggetti sani e pazienti affetti da Alzheimer (*Classificazione*).

> [!IMPORTANT]
> **Nota Architetturale: Approccio Modulare e Dominio di Riferimento Provvisorio**<br>
> Poiché i dataset definitivi non sono stati ancora rilasciati e l'assegnazione finale potrebbe essere libera o guidata, in questa **Fase di Costruzione dell'Infrastruttura** è stata adottata l'**Opzione 1 (Diabete di Tipo 1)** come **caso d'uso prototipale di riferimento**.
>
> **Motivazione dell'Opzione 1 per il Collaudo Iniziale:**
> - Impone vincoli architetturali più stringenti: la natura sequenziale dei dati (*Time Series*) e la criticità dei dosaggi consentono di testare la validazione temporale `[12 x 3]`, il runtime ONNX e i **Safety Guardrails** clinici applicativi (Clamp a 20.0 IU).
> - L'SLA dell'Opzione 1 (servizio attivo 7 giorni su 7, 6:00-22:00) è il più restrittivo e include pienamente le finestre operative delle Opzioni 2 e 3 (attive solo dal lunedì al venerdì, 8:00-18:00).
>
> **Garanzia di Portabilità Domain-Agnostic (Plug-and-Play):**<br>
> L'infrastruttura Cloud (cluster KinD multi-nodo, Dockerfile multi-stage non-root, Ingress NGINX con Rate Limiting, politiche di sicurezza Kubernetes, stress test `wrk` e script di deployment e validazione automatizzati pronti per l'integrazione CI/CD) è **completamente disaccoppiata dal tipo di modello**. 
> Qualora venga confermata l'Opzione 1 oppure assegnata l'Opzione 2 (Parkinson) o 3 (Alzheimer), l'**infrastruttura rimarrà identica al 100%**: risulterà sufficiente sostituire l'artefatto compilato `model/model.onnx` e aggiornare lo schema Pydantic per le nuove feature.

### Mappa dell'Architettura: dai Dati alla Risposta

```text
train_mock_model.py
  genera dati sintetici e compila il grafo del modello
        ↓ model.onnx + model_metadata.json
Dockerfile
  compila l'immagine container minimale e sicura (non-root)
        ↓ immagine caricata nei nodi worker KinD
Deployment → gestisce 3 repliche del pod con anti-affinity multi-nodo
        ↓
Ingress NGINX → Service (ClusterIP) → Pod FastAPI
                                        middleware → validazione Pydantic [12 x 3]
                                                   → inferenza ONNX Runtime C++
                                                   → safety clamp (<= 20.0 IU)
                                                   → risposta JSON + X-Request-ID
```

Il **Deployment** mantiene 3 repliche distribuite sui nodi worker; il **Service** fornisce un punto di accesso interno stabile; l'**Ingress NGINX** perimetra il cluster applicando rate limiting e instradamento. All'interno del container, il **middleware** misura la latenza e inietta header OWASP, **Pydantic** valida la matrice temporale e l'endpoint **/predict** esegue l'inferenza proteggendo l'output con il safety clamp clinico.

---

## 2. Mappatura Task della Traccia d'Esame (Tasks A – H)

La traccia ufficiale dell'esame definisce 8 compiti sequenziali. La tabella seguente illustra la corrispondenza esatta tra ciascun task accademico, il comando automatizzato e i file implementativi:

| Task Traccia | Descrizione Formale del Requisito | Comando Linux/macOS (Make) | Comando Windows (PowerShell) | File Chiave |
| :--- | :--- | :--- | :--- | :--- |
| **Task A** | Addestramento offline del modello ML | `make train` | `.\venv\Scripts\python.exe model/train_mock_model.py` | [`model/train_mock_model.py`](model/train_mock_model.py) |
| **Task B** | Salvataggio artefatto e costruzione Web App per inferenza | `make test` | `.\venv\Scripts\pytest.exe tests/ -v` | [`app/main.py`](app/main.py), [`app/schemas.py`](app/schemas.py) |
| **Task C** | Containerizzazione hardened con Docker Desktop | `make build` | `docker build -t diabetes-api:latest -f docker/Dockerfile .` | [`docker/Dockerfile`](docker/Dockerfile) |
| **Task D & E** | Analisi vulnerabilità CVE e patching con Docker Scout | `make scan` | `docker scout cves diabetes-api:latest` | [`docker/Dockerfile`](docker/Dockerfile) |
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
Verifica dei contratti dell'API, della forma della matrice $[12 \times 3]$ e del rifiuto di valori NaN/Inf:
```bash
make test
```

#### Passo 5: Compilazione immagine Docker e Scansione di Sicurezza (Task C, D, E)
Compilazione dell'immagine multi-stage con utente non-root (UID 10001) e scansione CVE con Docker Scout:
```bash
make build
make scan
```

#### Passo 6: Avvio del Cluster Kubernetes KinD (Task F)
Inizializzazione del cluster locale a 3 nodi (1 master + 2 worker) e applicazione dell'Ingress Controller NGINX:
```bash
make cluster-up
```

#### Passo 7: Deployment dell'applicazione sul Cluster (Task H)
Caricamento dell'immagine locale nei nodi KinD e rilascio del deployment a 3 repliche:
```bash
make deploy
```

#### Passo 8: Verifica degli Endpoint e Interfaccia Web Interattiva
Verifica diretta degli endpoint da riga di comando:
```bash
make test-endpoint
```
In alternativa, accesso alle interfacce web interattive tramite browser:
* **Documentazione Interattiva OpenAPI / Swagger UI:** [http://localhost/docs](http://localhost/docs)
* **Specifiche OpenAPI Redoc:** [http://localhost/redoc](http://localhost/redoc)
* **Probe di Liveness:** [http://localhost/health](http://localhost/health)
* **Probe di Readiness:** [http://localhost/ready](http://localhost/ready)

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
Usare gli stessi comandi Make del workflow Linux per build, deployment KinD e stress test:
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
docker build -t diabetes-api:latest -f docker/Dockerfile .
docker scout quickview diabetes-api:latest
docker scout cves diabetes-api:latest
```

#### Passo 4: Deployment Automatico sul Cluster
```powershell
.\scripts\deploy_k8s.ps1
```
*(Creazione del cluster KinD, caricamento dell'immagine locale, applicazione di NGINX Ingress e avvio delle repliche)*.

#### Passo 5: Test degli endpoint
```powershell
Invoke-RestMethod -Uri http://localhost/health
Invoke-RestMethod -Uri http://localhost/ready
```
Accesso a [http://localhost/docs](http://localhost/docs) per l'interazione grafica con le API.

#### Passo 6: Esecuzione Stress Test con `wrk` (tramite container Docker)
```powershell
.\scripts\run_stress_test.ps1
```
Lo script stampa le statistiche di `wrk`, ma non verifica automaticamente l'esito SLA: confrontare throughput, p99 e risposte non-2xx con le soglie indicate.

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
   L'esportazione del grafo da PyTorch a formato standard ONNX consente l'esecuzione ad alte prestazioni su CPU tramite ONNX Runtime C++, eliminando l'overhead di memoria del framework PyTorch in produzione (riduzione da ~800 MB a <100 MB di footprint) e garantendo latenze di inferenza sub-millisecondo ampiamente conformi all'SLA (< 1.0 s).
2. **Repliche e Alta Disponibilità Kubernetes:**
   La distribuzione di 3 repliche pod su 2 nodi worker tramite `podAntiAffinity` elimina il Single Point of Failure (SPOF). L'adozione del PodDisruptionBudget (PDB con `minAvailable: 2`) e della strategia di `RollingUpdate` (`maxSurge: 1`, `maxUnavailable: 0`) garantisce l'alta disponibilità continua e l'aggiornamento a zero-downtime, rispettando l'obiettivo di uptime del 99.5%.
3. **NGINX Ingress come Perimetro di Sicurezza:**
   Configurato come gateway perimetrale con Rate Limiting (150 RPS con burst 3x per supportare gateway NAT ospedalieri e picchi SLA), Connection Limiting (50 connessioni per IP contro Slowloris), restrizione verbi HTTP (solo GET e POST) e iniezione di header di sicurezza OWASP, proteggendo il perimetro del cluster prima che il traffico raggiunga il Service ClusterIP interno.

---

## 5. Architettura di Sicurezza & Hardening NGINX

| Controllo di Sicurezza | Modello di Minaccia & Motivazione | Meccanismo di Implementazione |
| :--- | :--- | :--- |
| **Container Non-Root** | Previene Container Breakout ed escalation dei privilegi sul nodo host. | `USER 10001:10001` in Dockerfile, `runAsNonRoot: true` in K8s Pod Security Context. |
| **Multi-Stage Build** | Riduce la superficie d'attacco; compilatori e strumenti di build sono esclusi dall'immagine finale. | Dockerfile a doppio stadio (`builder` -> `runner`), base image `python:3.11-slim`. |
| **Scansione CVE con Docker Scout** | Identifica Common Vulnerabilities and Exposures nel sistema operativo e nelle librerie Python prima del rilascio. | `docker scout cves` & `docker scout quickview`. |
| **Rate Limiting** | Previene attacchi Denial of Service (DoS) e garantisce la corretta ripartizione della capacità tra client concorrenti. | `nginx.ingress.kubernetes.io/limit-rps: "150"`, `limit-burst-multiplier: "3"`. |
| **Connection Limiting** | Mitiga attacchi Slowloris e l'esaurimento della tabella dei socket/connessioni sul gateway. | `nginx.ingress.kubernetes.io/limit-connections: "50"`. |
| **Dimensione Massima Body HTTP** | Previene attacchi di esaurimento memoria (Memory Starvation) dato che il payload clinico pesa < 2 KB. | `nginx.ingress.kubernetes.io/proxy-body-size: "1m"`. |
| **Restrizione Metodi HTTP** | Principio del Minimo Privilegio: sono ammessi solo `GET` (health/ready) e `POST` (predict). | NGINX snippet: `if ($request_method !~ ^(GET|POST|HEAD)$) { return 405; }`. |
| **Header HTTP OWASP** | Difesa in profondità (Defense-in-Depth) per imporre standard moderni di sicurezza HTTP. | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Strict-Transport-Security`. |
| **Quote di Risorse K8s** | Previene attacchi DoS interni e monopolio delle risorse CPU/RAM sul nodo host (Noisy Neighbor). | Limiti container: `cpu: 500m`, `memory: 256Mi`. |

### 5.1 Validazione dei Dati e Ciclo di Vita del Modello
- **Contratto dell'Input:** Pydantic v2 garantisce l'integrità strutturale della serie temporale `[12 x 3]`, bloccando valori fuori range fisiologico e valori non numerici (`NaN`, `Inf`) con `HTTP 422 Unprocessable Entity` prima dell'esecuzione dell'algebra lineare.
- **Safety Clamp Clinico:** L'output è vincolato rigidamente dal parametro `MAX_INSULIN_DOSE` (default `20.0 IU`) per prevenire somministrazioni eccessive in caso di anomalie dei sensori.
- **Readiness Gate:** In modalità `MODEL_MODE=real`, `/ready` restituisce `HTTP 503 Service Unavailable` se la sessione ONNX non è caricata in memoria, escludendo il pod dal routing prima di generare errori utente.
- **Tracciabilità e Checksum SHA256:** I pesi del modello sono associati a un hash crittografico SHA256 verificabile archiviato nel file di audit `model/model_metadata.json`.

---

## 6. Struttura del Repository e note sulle piattaforme

```
scc-project/
├── traccia.pdf              # Traccia ufficiale del progetto d'esame
├── docs/
│   └── feature_spec.md      # Specifiche tecniche formali del contratto dati [12 x 3]
├── .gitattributes           # Normalizzazione terminatori di riga (LF per Linux, CRLF per PowerShell)
├── .gitignore               # Esclusione cache, venv e file temporanei
├── Makefile                 # Automazione per Linux/macOS (Single Source of Truth)
├── requirements.txt         # Dipendenze minimali di runtime (FastAPI, ONNX Runtime)
├── requirements-train.txt   # Dipendenze offline per l'addestramento (PyTorch, ONNX)
├── requirements-test.txt    # Dipendenze per la suite di test (pytest, httpx)
├── app/
│   ├── main.py              # API FastAPI con runtime ONNX, safety clamp e probe rigorosi
│   ├── config.py            # Configurazioni via variabili d'ambiente (MODEL_MODE, MAX_DOSE)
│   └── schemas.py           # Validazione Pydantic v2 (range fisiologici, anti-NaN/Inf)
├── model/
│   ├── train_mock_model.py  # Script addestramento offline, metriche ed export ONNX
│   ├── model_metadata.json  # Metadati, versione, architettura e SHA256 checksum
│   └── model.onnx           # Artefatto modello esportato
├── docker/
│   ├── Dockerfile           # Multi-stage hardened (non-root UID 10001, superficie d'attacco CVE minimizzata)
│   └── .dockerignore
├── k8s/
│   ├── kind-config.yaml     # Topologia 1 Master + 2 Worker nodes KinD
│   ├── deployment.yaml      # 3 repliche richieste, probe e strategia di aggiornamento graduale
│   ├── pdb.yaml             # PodDisruptionBudget (minAvailable: 2) per interruzioni volontarie
│   ├── service.yaml         # ClusterIP interno
│   └── ingress.yaml         # Ingress NGINX con rate limiting (150 rps) e security headers
├── tests/
│   └── test_api.py          # Test automatizzati con pytest su health, ready e predict
└── scripts/
    ├── setup_host.sh / .ps1 # Setup strumenti di sistema (Docker, wrk, KinD, kubectl)
    ├── setup_host_macos.sh  # Setup macOS tramite Homebrew
    ├── setup_env.sh / .ps1  # Gestore VENV: profili CPU, CUDA e ROCm secondo piattaforma
    ├── deploy_k8s.sh / .ps1 # Deployment automatico idempotente (rollout restart)
    ├── run_stress_test.sh / .ps1 # wrk: confronto soglie su Linux/macOS, manuale su Windows
    └── test_predict.lua     # Script Lua che genera payload dimostrativi per wrk
```

### 6.1 Gestione Ambienti Virtuali per Hardware Eterogeneo (CPU, NVIDIA CUDA, AMD ROCm)

Per garantire massima flessibilità e portabilità tra postazioni di sviluppo e nodi di calcolo eterogenei (sistemi con GPU NVIDIA, GPU AMD Radeon o stazioni con sola CPU), il repository include un **gestore automatico degli ambienti virtuali** ([`scripts/setup_env.sh`](scripts/setup_env.sh) / [`scripts/setup_env.ps1`](scripts/setup_env.ps1)).

Su macOS è disponibile il profilo CPU; il rilevamento `auto` seleziona `cpu-train`, mentre i profili CUDA e ROCm sono rifiutati perché non supportati su questa piattaforma.

> [!NOTE]
> I binari NVIDIA CUDA e AMD ROCm non possono coesistere nel medesimo ambiente Python in quanto entrambi sovrascrivono i binding C++ di `torch`. Il gestore isola gli ambienti in directory dedicate sotto `.venvs/` e connette il profilo selezionato mediante il link simbolico `scc-project/venv`.

| Profilo | Dipendenze Installate | Dimensione | Scopo e Utilizzo |
| :--- | :--- | :--- | :--- |
| **`cpu-minimal`** | FastAPI, ONNX Runtime, Pydantic, uvicorn, onnx, pytest, httpx. **Zero PyTorch**. | **~100 MB** | Validazione rapida dell'API, build Docker, probe di liveness/readiness e cluster serving. |
| **`cpu-train`**<br>*(Consigliato)* | Tutto di `cpu-minimal` + PyTorch (wheel CPU su Linux, distribuzione standard su macOS) + scikit-learn + pandas. | **~350 MB** | **Profilo di riferimento per l'esame**: addestramento CPU su Linux e macOS. |
| **`gpu-cuda`** | Tutto di `cpu-minimal` + **PyTorch completo con NVIDIA CUDA 12.4** + torchvision + torchaudio. | **~2.5 GB** | Attivazione per ambienti dotati di GPU **NVIDIA** con supporto CUDA. |
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
