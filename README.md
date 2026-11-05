# Secure Cloud Computing - ML Serving SaaS on Kubernetes

**Language:** **English** | [Italiano](README.it.md)

[![Kubernetes](https://img.shields.io/badge/Kubernetes-KinD_v0.25-blue.svg)](https://kubernetes.io/)
[![NGINX](https://img.shields.io/badge/Gateway-NGINX_Ingress-green.svg)](https://kubernetes.github.io/ingress-nginx/)
[![Docker Scout](https://img.shields.io/badge/Security-Docker_Scout_Hardened-brightgreen.svg)](https://docs.docker.com/scout/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI_%2B_ONNX-teal.svg)](https://fastapi.tiangolo.com/)
[![Cross-Platform](https://img.shields.io/badge/Platform-Linux_%7C_macOS_%7C_Windows-orange.svg)](README.md)

---

## 1. Project Overview & Clinical Context

> **[Consult the official project assignment PDF](traccia.pdf)** for complete academic criteria and submission guidelines.

### The Three Official Assignment Options:
1. **Option 1:** Forecasting insulin intake for Type 1 Diabetes patients (*Time Series Forecasting*).
2. **Option 2:** Discriminating healthy subjects from Parkinson's disease patients (*Classification*).
3. **Option 3:** Discriminating healthy subjects from Alzheimer's disease patients (*Classification*).

> [!IMPORTANT]
> **Architectural Note: Modular Strategy & Reference Scaffolding Prototype**<br>
> Because final datasets will be released by course instructors after the Midterm break and final track assignment may be designated or elected, **Option 1 (Type 1 Diabetes)** has been implemented during this **Infrastructure Construction Phase** as the **reference benchmarking prototype**.
>
> **Rationale for Option 1 in Initial Prototyping:**
> - It establishes the most demanding architectural constraints: continuous sequential inputs (*Time Series*) and life-critical dosing outputs enable rigorous verification of sliding-window tensor validation `[12 x 3]`, ONNX Runtime execution, and application-level **Clinical Safety Guardrails** (hard clamp at 20.0 IU).
> - The SLA for Option 1 (active 7 days a week, 6:00 AM – 10:00 PM) is the most restrictive and strictly encompasses the business-hour SLAs of Options 2 and 3 (Monday–Friday, 8:00 AM – 6:00 PM).
>
> **Domain-Agnostic Guarantee (100% Plug-and-Play):**<br>
> The Cloud infrastructure (multi-node KinD cluster, hardened multi-stage Dockerfile, Ingress NGINX with rate-limiting, Kubernetes security contexts, `wrk` SLA automated stress testing, and automated deployment & validation scripts ready for CI/CD integration) is **completely decoupled from model internals**.
> Whether Option 1 is confirmed or Option 2 (Parkinson) or Option 3 (Alzheimer) is assigned, the **cloud infrastructure remains 100% identical**: replacing `model/model.onnx` and adjusting Pydantic schema validation for the new tabular features will serve the new medical domain immediately.

### System Architecture: from Data to Response

```text
train_mock_model.py
  generates synthetic data and compiles the model graph
        ↓ model.onnx + model_metadata.json
Dockerfile
  builds a minimal, secure container image (non-root UID 10001)
        ↓ image loaded into KinD worker nodes
Deployment → manages 3 pod replicas with multi-node anti-affinity
        ↓
Ingress NGINX → Service (ClusterIP) → FastAPI Pod
                                        middleware → Pydantic validation [12 x 3]
                                                   → C++ ONNX Runtime inference
                                                   → clinical safety clamp (<= 20.0 IU)
                                                   → JSON response + X-Request-ID
```

The **Deployment** maintains 3 replicas distributed across worker nodes; the **Service** provides a stable internal ClusterIP; **Ingress NGINX** perimeters the cluster with rate limiting and path-based routing. Inside the container, **middleware** tracks latency and injects OWASP headers, **Pydantic** validates temporal matrices, and the **/predict** endpoint executes inference while enforcing the clinical safety clamp.

---

## 2. Exam Assignment Task Mapping (Tasks A – H)

The official project assignment defines 8 sequential tasks. The table below maps each academic task to its automated command and primary source files:

| Task | Official Requirement Description | Linux/macOS Command (Make) | Windows Command (PowerShell) | Key Source Files |
| :--- | :--- | :--- | :--- | :--- |
| **Task A** | Train an ML model offline | `make train` | `.\venv\Scripts\python.exe model/train_mock_model.py` | [`model/train_mock_model.py`](model/train_mock_model.py) |
| **Task B** | Save final model & build Web App for inference | `make test` | `.\venv\Scripts\pytest.exe tests/ -v` | [`app/main.py`](app/main.py), [`app/schemas.py`](app/schemas.py) |
| **Task C** | Containerize the Web App and serve on Docker Desktop | `make build` | `docker build -t diabetes-api:latest -f docker/Dockerfile .` | [`docker/Dockerfile`](docker/Dockerfile) |
| **Task D & E** | Scan and patch CVE vulnerabilities using Docker Scout | `make scan` | `docker scout cves diabetes-api:latest` | [`docker/Dockerfile`](docker/Dockerfile) |
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
Validates API contracts, the $[12 \times 3]$ input shape, and rejection of NaN/Inf values:
```bash
make test
```

#### Step 5: Hardened Docker Image Compilation & Security Audit (Tasks C, D, E)
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

#### Step 8: Endpoint Verification & Interactive Documentation
Endpoints can be verified directly via CLI:
```bash
make test-endpoint
```
Alternatively, graphical interfaces and health probes are accessible via web browser:
* **Interactive OpenAPI / Swagger UI:** [http://localhost/docs](http://localhost/docs)
* **OpenAPI Redoc Specifications:** [http://localhost/redoc](http://localhost/redoc)
* **Liveness Probe:** [http://localhost/health](http://localhost/health)
* **Readiness Gate Probe:** [http://localhost/ready](http://localhost/ready)

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
docker build -t diabetes-api:latest -f docker/Dockerfile .
docker scout quickview diabetes-api:latest
docker scout cves diabetes-api:latest
```

#### Step 4: Automated Deployment to KinD Cluster
```powershell
.\scripts\deploy_k8s.ps1
```
*(Creates the KinD cluster, loads local images, deploys NGINX Ingress, and starts application pods)*.

#### Step 5: Endpoint Verification
```powershell
Invoke-RestMethod -Uri http://localhost/health
Invoke-RestMethod -Uri http://localhost/ready
```
Access [http://localhost/docs](http://localhost/docs) in a browser for interactive API inference.

#### Step 6: Dockerized Stress Testing with `wrk`
```powershell
.\scripts\run_stress_test.ps1
```
The script prints `wrk` statistics but does not automatically verify the SLA result; compare throughput, p99 latency, and non-2xx responses with the stated thresholds.

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
   Exporting the neural graph from PyTorch to standard ONNX enables high-performance execution on CPU via ONNX Runtime C++, eliminating production memory overhead (reduced from ~800 MB to <100 MB footprint) and ensuring sub-millisecond inference latency well within the SLA (< 1.0 s).
2. **Kubernetes Multi-Node High Availability:**
   Distributing 3 pod replicas across 2 worker nodes using `podAntiAffinity` eliminates Single Points of Failure (SPOF). Combining PodDisruptionBudget (PDB with `minAvailable: 2`) and zero-downtime RollingUpdate (`maxSurge: 1`, `maxUnavailable: 0`) guarantees continuous availability and satisfies the 99.5% uptime requirement.
3. **NGINX Ingress Perimeter Security:**
   Acts as an edge reverse proxy providing Rate Limiting (150 RPS with 3x burst multiplier to support hospital NAT gateways and SLA bursts), Connection Limiting (50 TCP connections per client IP against Slowloris), HTTP verb whitelisting (GET and POST only), and defensive OWASP security headers before traffic reaches the internal ClusterIP Service.

---

## 5. Security Architecture & NGINX Hardening

| Security Control | Threat Model & Rationale | Implementation Mechanism |
| :--- | :--- | :--- |
| **Non-Root Container** | Prevents container breakout and privilege escalation onto the host kernel. | `USER 10001:10001` in Dockerfile, `runAsNonRoot: true` in K8s Pod Security Context. |
| **Multi-Stage Build** | Minimizes attack surface; compilers and build toolchains are purged from final image. | Two-stage Dockerfile (`builder` -> `runner`), base image `python:3.11-slim`. |
| **Docker Scout CVE Scanning** | Identifies Common Vulnerabilities and Exposures in OS and Python libraries prior to deployment. | `docker scout cves` & `docker scout quickview`. |
| **Rate Limiting** | Prevents Denial of Service (DoS) and ensures fair resource distribution under high concurrency. | `nginx.ingress.kubernetes.io/limit-rps: "150"`, `limit-burst-multiplier: "3"`. |
| **Connection Limiting** | Mitigates Slowloris attacks and socket table exhaustion on the ingress gateway. | `nginx.ingress.kubernetes.io/limit-connections: "50"`. |
| **Maximum HTTP Body Size** | Prevents memory exhaustion (Memory Starvation) as valid time-series payloads weigh < 2 KB. | `nginx.ingress.kubernetes.io/proxy-body-size: "1m"`. |
| **HTTP Verb Restriction** | Principle of Least Privilege: only `GET` (health/ready) and `POST` (predict) allowed. | NGINX snippet: `if ($request_method !~ ^(GET|POST|HEAD)$) { return 405; }`. |
| **OWASP HTTP Security Headers** | Defense-in-Depth against MIME sniffing, clickjacking, and protocol downgrade. | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Strict-Transport-Security`. |
| **K8s Resource Quotas** | Prevents noisy-neighbor starvation and internal CPU/RAM exhaustion across worker nodes. | Container limits: `cpu: 500m`, `memory: 256Mi`. |

### 5.1 Data Validation and Model Lifecycle
- **Input Contract:** Pydantic v2 enforces structural integrity of the `[12 x 3]` time-series, immediately rejecting out-of-range clinical values and non-numeric entries (`NaN`, `Inf`) with `HTTP 422 Unprocessable Entity` before tensor multiplication.
- **Clinical Safety Clamp:** Application output is capped by `MAX_INSULIN_DOSE` (default `20.0 IU`) to prevent dangerous dosages in the event of abnormal sensor telemetry.
- **Readiness Gate:** In `MODEL_MODE=real`, `/ready` returns `HTTP 503 Service Unavailable` if the ONNX session is unallocated, excluding the pod from traffic before client errors occur.
- **Traceability & SHA256 Checksums:** Model weights are versioned offline with SHA256 cryptographic hashing in `model/model_metadata.json`.

---

## 6. Repository Structure and Platform Notes

```
scc-project/
├── traccia.pdf              # Official course assignment PDF
├── docs/
│   └── feature_spec.md      # Formal data contract specifications [12 x 3]
├── .gitattributes           # Line ending normalization (LF for Linux, CRLF for PowerShell)
├── .gitignore               # Ignored caches, venvs, and build artifacts
├── Makefile                 # Unified command automation for Linux/macOS (Single Source of Truth)
├── requirements.txt         # Minimal runtime dependencies (FastAPI, ONNX Runtime)
├── requirements-train.txt   # Offline training dependencies (PyTorch, ONNX)
├── requirements-test.txt    # Automated testing dependencies (pytest, httpx)
├── app/
│   ├── main.py              # FastAPI server with ONNX runtime, safety clamp & strict probes
│   ├── config.py            # Environment settings (MODEL_MODE, MAX_INSULIN_DOSE)
│   └── schemas.py           # Pydantic v2 validation contracts (physiological bounds, anti-NaN)
├── model/
│   ├── train_mock_model.py  # Offline training script, metrics calculation & ONNX export
│   ├── model_metadata.json  # Model metadata, version, architecture & SHA256 hash
│   └── model.onnx           # Exported ONNX computational graph artifact
├── docker/
│   ├── Dockerfile           # Multi-stage hardened build (non-root UID 10001, minimal CVE attack surface)
│   └── .dockerignore
├── k8s/
│   ├── kind-config.yaml     # KinD multi-node topology (1 Control-Plane + 2 Workers)
│   ├── deployment.yaml      # 3 requested replicas, probes, and rolling-update strategy
│   ├── pdb.yaml             # PodDisruptionBudget (minAvailable: 2) for voluntary disruptions
│   ├── service.yaml         # Internal ClusterIP load balancer
│   └── ingress.yaml         # NGINX Ingress with rate limiting (150 rps) & security headers
├── tests/
│   └── test_api.py          # Pytest automated test suite (health, ready, predict validation)
└── scripts/
    ├── setup_host.sh / .ps1 # Host provisioning (Docker, wrk, KinD, kubectl)
    ├── setup_host_macos.sh  # macOS host provisioning via Homebrew
    ├── setup_env.sh / .ps1  # Python VENV manager: CPU, CUDA, and ROCm profiles by platform
    ├── deploy_k8s.sh / .ps1 # Idempotent cluster deployment pipeline (rollout restart)
    ├── run_stress_test.sh / .ps1 # wrk: threshold comparison on Linux/macOS; manual on Windows
    └── test_predict.lua     # Lua script that generates demonstrative wrk payloads
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
