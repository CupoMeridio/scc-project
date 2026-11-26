# Secure Cloud Computing - ML Serving SaaS on Kubernetes

**Language:** **English** | [Italiano](README.it.md)

[![Kubernetes](https://img.shields.io/badge/Kubernetes-KinD_v0.25-blue.svg)](https://kubernetes.io/)
[![NGINX](https://img.shields.io/badge/Gateway-NGINX_Ingress-green.svg)](https://kubernetes.github.io/ingress-nginx/)
[![Container Hardening](https://img.shields.io/badge/Security-Non--Root_UID_10001-blue.svg)](docker/Dockerfile)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI_%2B_ONNX-teal.svg)](https://fastapi.tiangolo.com/)
[![Cross-Platform](https://img.shields.io/badge/Platform-Linux_%7C_macOS_%7C_Windows-orange.svg)](README.md)

---

## 1. Project Overview & Clinical Context

> **[Consult the official project assignment PDF](docs/traccia.pdf)** for complete academic criteria and submission guidelines.

### The Three Official Assignment Options:
1. **Option 1:** Predictive monitoring and early warning for Type 1 Diabetes patients (*Time Series Forecasting* & *Clinical Risk Classification*). *(SaMD note: as clarified by the instructor, pharmacological insulin dosing is strictly the physician's responsibility; the SaaS platform acts as an Early Warning System predicting 30-minute future glucose $G(t+6)$ and classifying risk into 7 clinical bands using the real OhioT1DM dataset).*
2. **Option 2:** Discriminating healthy subjects from Parkinson's disease patients (*Classification*).
3. **Option 3:** Discriminating healthy subjects from Alzheimer's disease patients (*Classification*).

> [!IMPORTANT]
> **Architectural Note: Modular Strategy & Reference Scaffolding Prototype**<br>
> Because final datasets will be released by course instructors after the Midterm break and final track assignment may be designated or elected, **Option 1 (Type 1 Diabetes)** has been implemented during this **Infrastructure Construction Phase** as the **reference benchmarking prototype**.
>
> **Rationale for Option 1 in Initial Prototyping:**
> - It establishes the most demanding architectural constraints: continuous sequential inputs (*Time Series*) and clinical criticality enable rigorous verification of temporal validation (13 glucose timesteps + 14 exogenous inputs), ONNX Runtime execution, and application-level **Physiological Safety Guardrails** (clamp [1.0, 35.0] mmol/L and 7 clinical risk classes).
> - The SLA for Option 1 (active 7 days a week, 6:00 AM – 10:00 PM) is the most restrictive and strictly encompasses the business-hour SLAs of Options 2 and 3 (Monday–Friday, 8:00 AM – 6:00 PM).
>
> **Domain-Agnostic Guarantee (100% Plug-and-Play):**<br>
> The Cloud infrastructure (multi-node KinD cluster, non-root multi-stage Dockerfile pinned by digest, Ingress NGINX with rate-limiting, Kubernetes security contexts, `wrk` SLA automated stress testing, and automated deployment & validation scripts ready for CI/CD integration) is **completely decoupled from model internals**.
> Whether Option 1 is confirmed or Option 2 (Parkinson) or Option 3 (Alzheimer) is assigned, the **cloud infrastructure remains 100% identical**: replacing `model/model.onnx` and adjusting Pydantic schema validation for the new tabular features will serve the new medical domain immediately.

### System Architecture: from Data to Response (Single-Center Multi-Role SaaS)

```text
Browser / Client (Patient, Diabetologist, Center Manager)
        ↓ Mandatory HTTPS (308 redirect from HTTP)
Ingress NGINX (TLS, Rate Limiting, OWASP Headers)
   ├── "/"               ──▶ Frontend (2 non-root NGINX replicas, classic HTML/CSS/JS + local Chart.js)
   └── "/api/*"          ──▶ SaaS API (2 FastAPI replicas: IAM HttpOnly Cookie, RBAC, Atomic Audit)
                               ├──▶ Redis Queue ──▶ Ingestion Worker (2 replicas) ──┐
                               ├──▶ PostgreSQL HA (CloudNativePG Primary + Standby) ◀┘
                               └──▶ ML Inference Service (internal ClusterIP, 3 ONNX replicas)
```

The **Ingress NGINX** gateway exposes only the web portal (`/`) and authenticated SaaS endpoints (`/api/*`) over mandatory HTTPS (`ssl-redirect: "true"` + `force-ssl-redirect: "true"`). The ML inference service (`diabetes-forecasting-service`, 3 replicas with `podAntiAffinity` and `PDB`) remains **strictly internal to the cluster**, protected by **`default-deny` + explicit allowlist NetworkPolicies**. CGM ingestion (`/api/cgm/ingest`) buffers events onto **Redis** (`202 Accepted`) and is processed idempotently by 2 **Ingestion Worker** replicas into **PostgreSQL HA (Primary + synchronous Standby via CloudNativePG, with daily `pg_dump` backups)**. The queue uses an in-flight list with explicit ack, retry and a dead-letter queue, so samples are not lost on worker crashes. Schema management uses **Alembic** migrations inside a dedicated **Kubernetes Job** (`k8s/seed-job.yaml`).

---

## 2. Exam Assignment Task Mapping (Tasks A – H)

The official project assignment defines 8 sequential tasks. The table below maps each academic task to its automated command and primary source files:

| Task | Official Requirement Description | Linux/macOS Command (Make) | Windows Command (PowerShell) | Key Source Files |
| :--- | :--- | :--- | :--- | :--- |
| **Task A** | Train an ML model offline | `make train` | `.\venv\Scripts\python.exe model/train_lstm.py` | [`model/train_lstm.py`](model/train_lstm.py) |
| **Task B** | Save final model & build Web App for inference | `make test` | `.\venv\Scripts\pytest.exe tests/ -v` | [`app/main.py`](app/main.py), [`app/schemas.py`](app/schemas.py) |
| **Task C** | Containerize the Web App and serve on Docker Desktop | `make build` | `docker build -t diabetes-api:1.0.0 -f docker/Dockerfile .` | [`docker/Dockerfile`](docker/Dockerfile), [`docker/Dockerfile.saas`](docker/Dockerfile.saas), [`docker/Dockerfile.frontend`](docker/Dockerfile.frontend) |
| **Task D & E** | Scan and patch CVE vulnerabilities using Docker Scout | `make scan` | `docker scout cves diabetes-api:1.0.0` | [`docker/Dockerfile`](docker/Dockerfile) |
| **Task F** | Create a Kubernetes cluster with 1 control node and 2+ workers | `make cluster-up` | `.\scripts\deploy_k8s.ps1` | [`k8s/kind-config.yaml`](k8s/kind-config.yaml) |
| **Task G** | Benchmark `wrk` and automated SLA verification on Linux/macOS | `make stress-test` | `.\scripts\run_stress_test.ps1` | [`scripts/run_stress_test.sh`](scripts/run_stress_test.sh), [`scripts/test_predict.lua`](scripts/test_predict.lua) |
| **Task H** | Deploy the final Web App on the Kubernetes cluster | `make deploy` | `.\scripts\deploy_k8s.ps1` | [`k8s/deployment.yaml`](k8s/deployment.yaml), [`k8s/ingress.yaml`](k8s/ingress.yaml) |

---

## 3. Step-by-Step Execution Guide (Quickstart)

### Technical System Prerequisites
Ensure the host environment satisfies the following minimum prerequisites prior to execution:
* **Linux:** Bash shell, `make`, `git`, `curl`, and `sudo`. The `make setup` script uses APT and the Debian 13 (Trixie) repository; on Ubuntu, Fedora, and other distributions, install prerequisites with the appropriate package manager.
* **macOS:** Homebrew, Bash, `make`, `git`, and `curl`. `make setup` installs Docker Desktop, KinD, kubectl, `wrk`, `jq`, and Python 3.
* **Windows (10/11):** **Docker Desktop** running with WSL2 backend enabled, and **PowerShell**.

---

### Dataset Provisioning (OhioT1DM — Decision D1)

The clinical dataset is **not** distributed with this repository and is **not** baked into any container image. The GitHub repository is public and OhioT1DM is released under a data-usage agreement that forbids redistribution.

* **Source:** the official OhioT1DM `GlucoseClassification` bundle, obtained under that agreement (12 patients, 24 CSV files).
* **Where to place it (local):** at the repository root, so the layout is `Diabetes/GlucoseClassification/<patient_code>/<patient_code>-ws-testing(t+30).csv` (and `-ws-training(t+30).csv`). This path is read directly by `make train` and `make simulate-cgm`.
* **How it reaches the seed Job (in-cluster):** [`k8s/kind-config.yaml`](k8s/kind-config.yaml) declares `extraMounts`, mapping the local `Diabetes/GlucoseClassification/` folder to `/mnt/ohiot1dm` on every KinD node. The demo seed Job ([`k8s/seed-demo-job.yaml`](k8s/seed-demo-job.yaml)) mounts that path read-only and reads it through the `DATASET_DIR` environment variable. `hostPath` volumes ignore `fsGroup`, so the CSV files must be world-readable (e.g. `chmod -R a+r Diabetes`).
* **Without the local folder:** `make train` cannot run and `make seed-demo` still creates the demo accounts but logs a warning and loads no CGM history.

---

### Option A: Complete Workflow on Debian 13

#### Step 1: System Tool Provisioning (Docker, wrk, KinD, kubectl)
Installs Docker CE, Docker Scout CLI, KinD, kubectl, wrk, and python3-venv, adding the execution user to the `docker` system group:
```bash
make setup
```
> [!IMPORTANT]
> If Docker was freshly installed, activate the newly granted group membership in the current shell:
> ```bash
> newgrp docker
> ```

#### Step 2: Python Virtual Environment Configuration (Hardware Acceleration)
Scans host GPU hardware (AMD Radeon, NVIDIA CUDA, or CPU-only) and configures the environment under `.venvs/`:
```bash
make env-auto
```
*(Alternatively, the standard CPU profile can be selected explicitly via `make env-cpu-train`)*.

#### Step 3: Offline Model Training (Task A)
Executes neural network training and exports `model/model.onnx` along with SHA256 cryptographic metadata in `model/model_metadata.json`:
```bash
make train
```

#### Step 4: Automated Verification Suite (Task B)
Validates API contracts, clinical input shapes (13 glucose readings and 14 exogenous features), and rejection of invalid or NaN/Inf values:
```bash
make test
```

#### Step 5: Multi-Stage Docker Image Compilation & Security Audit (Tasks C, D, E)
Builds the non-root multi-stage image (UID 10001) and executes Docker Scout CVE vulnerability inspection:
```bash
make build
make scan
```

#### Step 6: Multi-Node KinD Kubernetes Cluster Initialization (Task F)
Provisions the 3-node cluster topology (1 control-plane + 2 workers) and configures the NGINX Ingress Controller:
```bash
make cluster-up
```

#### Step 7: Application Deployment to Cluster (Task H)
Loads the locally built container image into KinD nodes and executes the 3-replica rolling deployment:
```bash
make deploy
```

#### Step 8: Endpoint Verification & HTTPS Web Portal
Verify public HTTPS endpoints, HTTP->HTTPS redirect, and internal ML isolation via CLI:
```bash
make test-endpoint
```
Access the web portal over HTTPS (with HttpOnly session cookies and server-side RBAC for Patient, Diabetologist, and Center Manager):
* **SaaS Web Portal (Login & Dashboards):** [https://localhost](https://localhost)
* **SaaS Health / Readiness:** [https://localhost/api/health](https://localhost/api/health) / [https://localhost/api/ready](https://localhost/api/ready)
* **Optional Demo Seed Job:** `make seed-demo` — generates random demo passwords and prints them once to the terminal. The seed Job applies them when it creates the demo accounts (the database must be empty). No demo credentials are stored in the repository or applied by `make deploy`.

#### Step 8b: CGM Sensor Stream Simulation (OhioT1DM Replay)
To run the in-process replay (no cluster needed):
```bash
make simulate-cgm
```
`/api/cgm/ingest` is an internal endpoint (D2): it is **not** reachable through the Ingress, so ingestion must go through a port-forward and requires a per-device key sent as the `X-Device-Key` header. The key is stored in the `device-keys` Secret created by the seed Job (one entry per patient, decision N1/Q3):
```bash
# In one terminal, forward the SaaS service:
kubectl port-forward svc/diabetes-saas-service 8080:80
# In another terminal, ingest using the patient's device key:
make simulate-cgm-ingest PATIENT=591
# Or manually:
python scripts/simulate_cgm_patient.py --ingest --endpoint http://127.0.0.1:8080/predict --patient 591 \
  --device-key "$(kubectl get secret device-keys -o jsonpath='{.data.patient_591}' | base64 -d)"
```
On Windows, `scripts/simulate_cgm_ingest.ps1` performs the same flow (reads the key from the Secret, opens the port-forward, runs the simulator).

#### Step 9: Load Stress Testing & SLA Verification (Task G)
Executes the automated `wrk` benchmark pipeline to validate all SLA criteria established by the project specification (Throughput $\ge 83.33\text{ RPS}$, Latency $p99 \le 1000\text{ ms}$, Error Rate $< 0.5\%$, corresponding to $99.5\%$ availability):
```bash
make stress-test
```
On Windows, `run_stress_test.ps1` runs `wrk` within a lightweight Docker container, displaying throughput and percentile latency statistics.

#### Step 10: Aggregated Log Inspection & Cluster Teardown
* Real-time aggregated log streams across all 3 replicas can be inspected via:
  ```bash
  make logs
  ```
* Upon completion of testing activities, destroy the KinD cluster to release host memory:
  ```bash
  make cluster-down
  ```

---

### Option B: Complete Workflow on macOS (Docker Desktop + Homebrew)

#### Step 1: Install host tools
With Homebrew installed, provision Docker Desktop, KinD, kubectl, `wrk`, and `jq`:
```bash
make setup
```
Launch Docker Desktop and wait for its engine to become ready:
```bash
open -a Docker
docker info
```

#### Step 2: Configure Python and run tests
On macOS, automatic hardware detection selects the CPU profile; CUDA and ROCm are unsupported:
```bash
make env-auto
make train
make test
```

#### Step 3: Build and deploy
Use the same Make commands as the Linux workflow to build, deploy to KinD, and run the stress test:
```bash
make build
make scan
make deploy
make test-endpoint
make stress-test
```

---

### Option C: Complete Workflow on Windows (Docker Desktop + PowerShell)

#### Step 1: Tool Installation (KinD and kubectl via winget)
Execute the host provisioning script in PowerShell:
```powershell
.\scripts\setup_host.ps1
```

#### Step 2: Python Virtual Environment Setup
Configure the virtual environment via automated detection:
```powershell
.\scripts\setup_env.ps1 auto
```

#### Step 3: Container Build & Docker Scout Audit
```powershell
docker build -t diabetes-api:1.0.0 -f docker/Dockerfile .
docker build -t diabetes-saas-api:1.0.0 -f docker/Dockerfile.saas .
docker build -t diabetes-frontend:1.0.0 -f docker/Dockerfile.frontend .
docker scout quickview diabetes-api:1.0.0
docker scout cves diabetes-api:1.0.0
```

#### Step 4: Automated Deployment to KinD Cluster
```powershell
.\scripts\deploy_k8s.ps1
```
*(Creates the KinD cluster, loads `:1.0.0` images, deploys NGINX Ingress v1.15.1, CloudNativePG HA, Redis, Alembic Seed Job, SaaS API, Worker, and Frontend)*.

#### Step 5: Endpoint Verification
```powershell
curl.exe -s -o NUL -w "%{http_code}\n" http://localhost/          # expected 308
curl.exe -s -k -o NUL -w "%{http_code}\n" https://localhost/predict  # expected 404 (internal only)
curl.exe -s -k https://localhost/api/health
curl.exe -s -k https://localhost/api/ready
```
Access [https://localhost](https://localhost) in a browser for the authenticated SaaS portal.

#### Step 6: Dockerized Stress Testing with `wrk`
```powershell
.\scripts\run_stress_test.ps1
```
By default (`-TargetHost internal`), the script opens a `kubectl port-forward` to the internal ML service, saves the raw output to `docs/audit_reports/wrk_<timestamp>.txt`, and verifies throughput $\ge 83.33$ RPS, p99 latency $\le 1000$ ms, and error rate $\le 0.50\%$, exiting non-zero if any check fails.

---

## 4. Service Level Agreement (SLA) & Capacity Planning

The official assignment specifies operational availability windows based on project domain:
- **Operating Uptime:**
  - *Option 1 (Diabetes):* 6:00 AM to 10:00 PM daily, **7 days a week**.
  - *Options 2 & 3 (Parkinson / Alzheimer):* 8:00 AM to 6:00 PM daily, **Monday – Friday**.
- **Customer Base:** Approximately **50,000 registered patients/users**.
- **Peak Load:** Up to **5,000 requests per minute** (as defined by the assignment; this does not mean 5,000 simultaneous requests).
- **Target Throughput:** $\frac{5000 \text{ req}}{60 \text{ s}} \approx \mathbf{83.33 \text{ RPS}}$ (Requests Per Second).
- **Response Time (Latency):** $\le \mathbf{1.0 \text{ second}}$ per request.
- **Required Availability:** $\ge \mathbf{99.5\%}$ of operating time, to be assessed over an appropriate period.
- **Outage Tolerance:** Maximum single outage 30 minutes, average recovery time (MTTR) $\le \mathbf{3 \text{ minutes}}$.

### Key Engineering Decisions:
1. **FastAPI + Uvicorn + ONNX Runtime:**
   Exporting the neural graph from PyTorch to standard ONNX enables high-performance execution on CPU via ONNX Runtime C++, substantially reducing runtime memory overhead compared to bundling the full PyTorch training framework in production and ensuring response times well within the SLA target (< 1.0 s).
2. **Kubernetes Multi-Node Pod Resilience:**
   Distributing 3 pod replicas across 2 worker nodes using `podAntiAffinity` (preferred) enhances application fault tolerance. Combining PodDisruptionBudget (PDB with `minAvailable: 2`) and zero-downtime RollingUpdate (`maxSurge: 1`, `maxUnavailable: 0`) guarantees continuous availability and satisfies the 99.5% uptime requirement.
3. **NGINX Ingress Perimeter Security & TLS Termination:**
   Acts as an edge reverse proxy providing Rate Limiting (1000 RPS with 5x burst multiplier per IP, intentionally dimensioned ~12x above the 83.33 RPS target so single-IP `wrk` tests and clinic traffic do not trigger false 429s), Connection Limiting (150 TCP connections per client IP against Slowloris), TLS termination on port 443 with self-signed certificate and SAN (`DNS:localhost, IP:127.0.0.1`), mandatory HTTPS redirect (`ssl-redirect: "true"` and `force-ssl-redirect: "true"`), and no custom NGINX snippet annotations (`allow-snippet-annotations=false`, D3). Write methods (`PUT`/`DELETE`) are rejected (`405`) by `frontend/nginx.conf` on `/` and FastAPI routing on `/api/*`.

---

## 5. Security Architecture & Perimeter Controls

| Security Control | Threat Model & Rationale | Implementation Mechanism |
| :--- | :--- | :--- |
| **Non-Root Container** | Prevents container breakout and privilege escalation onto the host kernel. | `USER 10001:10001` (`101` for NGINX unprivileged), `runAsNonRoot: true`, `readOnlyRootFilesystem: true`. |
| **Multi-Stage Build** | Minimizes attack surface; compilers and build toolchains are purged from final image. | Two-stage Dockerfile (`builder` -> `runner`), base image pinned by digest (`python:3.11.11-slim@sha256:081075da…`). |
| **Docker Scout CVE Scanning** | Identifies Common Vulnerabilities and Exposures in OS and Python libraries prior to deployment. | `docker scout cves` & `docker scout quickview` (saved in `docs/audit_reports/`). |
| **Rate Limiting** | Prevents Denial of Service (DoS) and ensures fair resource distribution under high concurrency. | `nginx.ingress.kubernetes.io/limit-rps: "1000"`, `limit-burst-multiplier: "5"`. |
| **Connection Limiting** | Mitigates Slowloris attacks and socket table exhaustion on the ingress gateway. | `nginx.ingress.kubernetes.io/limit-connections: "150"`. |
| **TLS / HTTPS Termination** | End-to-end encryption of in-transit biometric data (HIPAA & GDPR Art. 32). | Secret `diabetes-tls-cert` on port 443, `ssl-redirect: "true"`, `force-ssl-redirect: "true"`. |
| **Maximum HTTP Body Size** | Prevents memory exhaustion (Memory Starvation) as valid time-series payloads weigh < 2 KB. | `nginx.ingress.kubernetes.io/proxy-body-size: "1m"`. |
| **HTTP Verb Restriction** | Principle of Least Privilege: static portal permits `GET`/`HEAD` only (`405` otherwise); SaaS API routes only valid verbs (`405` on `PUT`/`DELETE`). | Enforced natively in `frontend/nginx.conf` and FastAPI routers (zero Ingress snippets). |
| **OWASP HTTP Security Headers** | Defense-in-Depth against MIME sniffing, clickjacking, XSS, and protocol downgrade. | Injected by `frontend/nginx.conf` and FastAPI middleware (`X-Frame-Options`, `X-Content-Type-Options`, `CSP`, `HSTS`). |
| **K8s Resource Quotas** | Prevents noisy-neighbor starvation and internal CPU/RAM exhaustion across worker nodes. | Container limits set across ML (`500m`/`256Mi`), SaaS, Worker, Redis, and PostgreSQL HA pods. |

### 5.1 Data Validation and Model Lifecycle
- **Input Contract:** Pydantic v2 enforces structural integrity of the time-series (13 glucose measurements in mmol/L and 14 exogenous features), immediately rejecting out-of-range clinical values and non-numeric entries (`NaN`, `Inf`) with `HTTP 422 Unprocessable Entity` before tensor multiplication.
- **Physiological Safety Clamp & Early Warning:** Application output is bounded by $[1.0, 35.0]\text{ mmol/L}$ and categorized across 7 risk classes to warn patients of pending hypo/hyperglycemia.
- **Fail-Closed Readiness Gate:** `/ready` returns `HTTP 503 Service Unavailable` if the ONNX session or scaler normalization parameters are unallocated or compromised, excluding the pod from traffic before client errors occur.
- **Supply Chain Security & SHA256 Checksums:** Model artifacts (`model.onnx` and `model.onnx.data`) are verified at startup against cryptographic hashes stored in `model/model_metadata.json`.
- **Clinical Scope — Not a Medical Device, No Insulin Dosing:** The service forecasts glucose **30 minutes ahead** into 7 risk classes as early-warning decision support only. It does **not** compute, recommend, or validate insulin doses or carbohydrate ratios; insulin-on-board and bolus calculation are explicitly out of scope and must never be derived from this output. It is a prototyping artifact, not a certified medical device (no CE/FDA marking), and does not replace clinical judgement.
- **Model Limitations (declared, not hidden):** The LSTM is trained on the small, single-center **OhioT1DM** cohort using a 13-step CGM history plus 14 exogenous features. Accuracy degrades with missing/sparse sensor data, unannounced meals, exercise, and patients outside the training distribution. MAE and bias are reported only when at least one forecast/ground-truth pair exists; otherwise the UI shows "insufficient data" rather than a fabricated number.

### 5.2 Ingress NGINX Lifecycle Status & Dataset Usage
- **Ingress NGINX is retained with mitigations (Decision D3):** The project pins `ingress-nginx/controller:v1.15.1` by digest, the last community release before `kubernetes/ingress-nginx` was archived on 24 March 2026. Because no fixes will be issued for later vulnerabilities (e.g. CVE-2026-116472), snippet annotations are disabled (`allow-snippet-annotations=false`, mitigating CVE-2021-25742) and the controller image is digest-pinned. **Gateway API** is documented as the future migration path; the ingress layer is intentionally isolated from the application so it can be replaced without touching business logic.
- **OhioT1DM dataset usage (Decision D1):** The clinical dataset is **not redistributed** in this public repository. It is used under the OhioT1DM data usage agreement and is mounted read-only into the seed Job from a local volume (`k8s/seed-demo-job.yaml`); neither the Git history nor any container image contains the CSV files.

---

## 6. Repository Structure and Platform Notes

```
scc-project/
├── docs/
│   ├── traccia.pdf                       # Official course assignment PDF
│   ├── Analisi architettura funzionale/  # SaaS functional requirements report and diagrams
│   └── Architettura cloud/               # Cloud architecture report and diagrams
├── Diabetes/                # (local only, NOT committed) OhioT1DM CSVs mounted read-only by the seed Job (D1)
├── .dockerignore            # Exclusions for container build contexts
├── .gitattributes           # Line ending normalization (LF for Linux, CRLF for PowerShell)
├── .gitignore               # Ignored caches, venvs, and build artifacts
├── Makefile                 # Unified command automation for Linux/macOS
├── requirements.txt         # Minimal ML runtime dependencies (FastAPI, ONNX Runtime)
├── requirements.lock        # Hash-pinned lock for the ML image (pip-compile --generate-hashes)
├── requirements-saas.txt    # SaaS backend dependencies (FastAPI, SQLAlchemy, psycopg, PyJWT, bcrypt)
├── requirements-saas.lock   # Hash-pinned lock for the SaaS/Worker/Seed image
├── requirements-train.txt   # Offline training dependencies (PyTorch, ONNX)
├── requirements-test.txt    # Automated testing dependencies (pytest, httpx)
├── common/
│   └── __init__.py          # Shared clinical thresholds (CLASS_BINS, classify_glucose)
├── app/                     # Internal ONNX inference microservice
├── saas_app/                # SaaS IAM/RBAC backend (services, repositories, atomic audit, seed_cli)
├── frontend/                # Classic HTML/CSS/JS portal + local Chart.js + non-root NGINX
├── model/                   # Offline LSTM training, metadata & ONNX artifacts
├── docker/                  # Non-root multi-stage Dockerfiles (ML, SaaS API+Worker+Seed, Frontend)
├── k8s/                     # KinD config, CloudNativePG HA, Redis, Worker, Seed Job, Deployments (:1.0.0), PDBs, default-deny NetworkPolicies, Ingress
├── tests/                   # Automated tests (test_api.py + test_saas_api.py)
├── docs/audit_reports/      # Raw, unedited audit evidence: image digests, Scout/wrk outputs (D8)
└── scripts/                 # Host provisioning, KinD deployment, CGM replay & internal wrk SLA benchmark
```

### 6.1 Virtual Environment Management for Heterogeneous Hardware (CPU, NVIDIA CUDA, AMD ROCm)

To ensure seamless portability across diverse developer workstations and compute environments (systems with NVIDIA GPUs, AMD Radeon GPUs, or standard CPU nodes), the project includes an **automated virtual environment manager** ([`scripts/setup_env.sh`](scripts/setup_env.sh) / [`scripts/setup_env.ps1`](scripts/setup_env.ps1)).

On macOS, the CPU profiles are available; `auto` selects `cpu-train`. CUDA and ROCm profiles are rejected because these runtimes are not supported on this platform.

> [!NOTE]
> NVIDIA CUDA and AMD ROCm binaries cannot coexist in the same Python environment because both override the `torch` C++ bindings. The environment manager isolates runtime profiles under `.venvs/` and links the active profile via the symbolic link `scc-project/venv`.

| Profile | Installed Packages | Disk Size | Target Hardware & Use Case |
| :--- | :--- | :--- | :--- |
| **`cpu-minimal`** | FastAPI, ONNX Runtime, Pydantic, uvicorn, onnx, pytest, httpx. **Zero PyTorch**. | **~100 MB** | Lightweight API validation, Docker builds, health probe tests, and Kubernetes serving. |
| **`cpu-train`**<br>*(Recommended)* | Everything in `cpu-minimal` + PyTorch (CPU wheel on Linux, standard distribution on macOS) + scikit-learn + pandas. | **~350 MB** | **Default project profile**: CPU training on Linux and macOS. |
| **`gpu-cuda`** | Everything in `cpu-minimal` + **Full PyTorch with NVIDIA CUDA 12.4** + torchvision + torchaudio. | **~2.5 GB** | Dedicated environments on systems equipped with an **NVIDIA** GPU. |
| **`gpu-rocm`** | On Linux: PyTorch with ROCm as supported by the setup script. On Windows: currently installs PyTorch CPU, not ROCm. | Varies | Use ROCm acceleration only on explicitly supported platforms; detecting an AMD GPU on Windows does not enable acceleration. |
| **`auto`** | Automatically detects GPU hardware on Linux; selects `cpu-train` on macOS. | Dynamic | **Platform-aware profile selection**. |

**Architectural Benefit:** Once initialized, switching profiles across hardware configurations requires **1 millisecond** (updating the symbolic link) without redownloading packages.

---

## 7. Troubleshooting & Frequently Asked Questions (FAQ)

### Q: "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
* **Cause:** The background Docker system daemon is not running.
* **Solution (Linux):** Start the Docker service via:
  ```bash
  sudo systemctl start docker
  ```
* **Solution (Windows):** Launch the **Docker Desktop** application and await steady operational status (green whale icon).
* **Solution (macOS):** Launch Docker Desktop with `open -a Docker` and verify the engine with `docker info`.

### Q: "permission denied while trying to connect to the Docker daemon socket" on Linux
* **Cause:** The current user session has not loaded the `docker` system group.
* **Solution:** Execute `newgrp docker` in the active shell or re-authenticate the user session:
  ```bash
  newgrp docker
  ```

### Q: "failed to bind port 0.0.0.0:80: address already in use"
* **Cause:** An existing web server daemon (such as Apache or NGINX on the host) is bound to port 80.
* **Solution:** Identify the conflicting process and terminate the host service:
  ```bash
  sudo ss -tulpn | grep :80
  sudo systemctl stop apache2 2>/dev/null || sudo systemctl stop nginx 2>/dev/null
  ```

### Q: How can Kubernetes nodes, pods, and ingress routing be inspected manually?
Standard `kubectl` inspection commands can be executed at any time:
```bash
kubectl get nodes -o wide      # Displays all 3 cluster nodes (master + 2 workers) and internal IPs
kubectl get pods -o wide       # Shows all 3 API replicas and their assigned worker nodes
kubectl get ingress            # Inspects NGINX Ingress routing rules and annotations
kubectl get services           # Inspects the internal ClusterIP service
```
