# Mappa di corrispondenza progettazione / implementazione

Data aggiornamento: 27 novembre 2026

## Criteri di stato

- **Fatto**: implementato e coperto da test o script assertivo.
- **Parziale**: implementato in parte oppure privo di una prova ripetibile.
- **Da verificare su infrastruttura**: richiede esecuzione live su Kubernetes.
- **Non realizzabile su KinD**: deliberata differenza mantenuta per limiti della demo locale.

## Corrispondenza principale

| Voce PDF | Implementazione attuale | Stato | Evidenza / limite |
|---|---|---|---|
| Zone IAM, Clinical, Manager, Ingest, Training e ML | Deployment separati e immagine SaaS condivisa selezionata con `SERVICE_MODE` | Fatto | `k8s/iam-deployment.yaml`, `k8s/clinical-deployment.yaml`, `k8s/manager-deployment.yaml`, `k8s/ingest-deployment.yaml`, `k8s/training-cronjob.yaml`, `k8s/deployment.yaml` |
| NGINX Ingress come gateway HTTPS | Routing per path, HTTPS obbligatorio, backend HTTPS, nessuno snippet | Fatto | `k8s/ingress.yaml`; `make verify-netpol` |
| Verifica dell'identità e del ruolo | JWT Ed25519 verificati localmente da Clinical e Manager; cookie HttpOnly | Fatto | `saas_app/auth.py`, `saas_app/routers/auth.py`; test SaaS |
| IAM raggiungibile solo dal load balancer | Manager non ha credenziali o rete verso IAM; Clinical usa solo provisioning dedicato | Fatto | `k8s/networkpolicy.yaml`; report NetworkPolicy |
| Isolamento del database per ruolo | Sei ruoli PostgreSQL con privilegi dedicati e viste aggregate/pseudonimizzate | Fatto | `k8s/postgres-ha.yaml`; `make verify-db-roles`; `make test-pg` |
| Vista aggregata manager | Il manager legge solo le viste `v_manager_*` | Fatto | `saas_app/services/manager_service.py`; test PostgreSQL |
| Zona training con dati pseudonimizzati | Trainer legge solo `v_training_cgm_series` tramite ruolo dedicato | Fatto | `k8s/training-cronjob.yaml`; `saas_app/repositories.py` |
| Training Champion/Challenger | Split temporale, confronto ONNX, soglia di promozione e bootstrap 95% | Fatto | `model/retrain_pipeline.py`; job live mantenuto con Champion 1.0.0 e Challenger 1.0.1 non promosso |
| Firma del manifesto modello | Manifesto Ed25519; chiave privata nel trainer e pubblica nel pod ML | Fatto | `common/model_signing.py`; test zone |
| Repository del modello e fail-closed | Artefatti su PVC, pubblicazione autorizzata e verifica hash/firma prima dell'avvio | Fatto | `saas_app/model_repo.py`, `k8s/model-repo.yaml`, `app/main.py` |
| HPA ML da 3 a 6 repliche | HPA Kubernetes con soglia CPU 70% | Fatto | `k8s/hpa.yaml`; benchmark live con 3 repliche iniziali |
| HPA sugli altri servizi online | HPA presenti per IAM, Clinical, Manager, Ingest e Frontend | Fatto | `k8s/hpa.yaml`; richiede verifica sotto carico |
| Alta disponibilità | PDB, anti-affinity, rolling update `maxUnavailable: 0` | Fatto | manifest Deployment/PDB; rollout live da verificare |
| Coda CGM | Redis interno con worker dedicato e ack/retry/DLQ | Parziale | `saas_app/worker.py`, `saas_app/queue_client.py`; Redis non ancora HA |
| Autenticazione dispositivi CGM | Chiavi casuali conservate solo come hash; limite di frequenza | Fatto | `saas_app/models.py`, `saas_app/routers/ingestion.py`, `saas_app/auth.py` |
| Ingestione sensori | Mantiene D2: endpoint interno, non esposto dall'Ingress | Fatto | `k8s/ingress.yaml`, `k8s/networkpolicy.yaml` |
| Cifratura backup | X25519 + HKDF-SHA256 + AES-256-GCM; chiave privata fuori dal cluster | Fatto | `saas_app/backup_crypto.py`; `make verify-backup` |
| Riproducibilità del deploy | Ogni esecuzione di `make deploy` o `deploy_k8s.ps1` elimina e ricrea il cluster, senza riutilizzare dati, PVC, Secret o password DB | Fatto | `scripts/deploy_k8s.sh`, `scripts/deploy_k8s.ps1`; perdita completa dello stato applicativo prevista |
| TLS interno | TLS su tutti e 7 i servizi interni (CA `internal-ca`), backend Ingress HTTPS (`backend-protocol: HTTPS`) e PostgreSQL `sslmode=verify-full` | Fatto | `scripts/deploy_k8s.sh`, `k8s/ingress.yaml`, `k8s/postgres-ha.yaml`, `k8s/redis.yaml` |
| Audit append-only | Trigger SQL impedisce UPDATE, DELETE e TRUNCATE | Fatto | migrazione Alembic; `make test-pg`; `make verify-db-roles` |
| Controllo freschezza CGM | Dati assenti, troppo vecchi o non consecutivi producono `NO_RECENT_DATA` | Fatto | `saas_app/services/patient_service.py` |
| Monitoraggio SLA | `/metrics` per pod ML e dashboard Manager | Parziale | manca alerting automatico; nessun monitor esterno persistente |
| Storage time series separato | PostgreSQL operativo unico con indice temporale e ruoli separati | Non realizzabile su KinD / miglioramento dichiarato | La separazione fisica può essere introdotta in produzione con partizionamento o TimescaleDB |
| Ingestione pubblica da Internet | Mantenuta solo interna perché D2 è esplicita e la demo usa simulatori | Divergenza motivata | Evita esposizione inutile di un dispositivo medico privo di mTLS hardware |
| Storage dati cifrato a riposo | Backup cifrati; cifratura del volume dati non verificabile nella demo KinD | Parziale / da produzione | Dichiarata come differenza rispetto al PDF |
| Coda ad alta disponibilità | Redis attualmente mono-replica | Da verificare / miglioramento futuro | Stato adeguato solo alla risorsa del portatile; Sentinel può essere valutato dopo le prove SLA |
| SLA monitor completo | `/metrics` ML + test ripetuti su servizio ML (3 run fisse + HPA 3→6) e percorso completo Ingress→Clinical→ML→PG (`POST /api/patient/meals`) | Parziale | Manca alerting automatico esterno; prove `wrk` complete in `docs/audit_reports/` (`103.24 RPS`, p99 `883.76 ms` end-to-end; `1674.59 RPS` media ML) |
| Test browser | Verifica end-to-end via curl/TestClient eseguita (`V1` con `IAM=0`, cambio password, cookie `__Host-scc_session`) | Da verificare | Rendering HTML/JS/Chart.js e flusso interattivo da aprire nel browser |

## Evidenza SLA e spiegazione varianze (V2)

- **Benchmark interno ML (`diabetes-forecasting-service`, TLS interno 443, repliche fisse 3/3):**
  - 3 run consecutive `20s`, `-t4 -c100`: `1628.17 RPS` (`p99 346.28 ms`), `1706.20 RPS` (`p99 96.52 ms`), `1689.40 RPS` (`p99 96.50 ms`) → **media `1674.59 RPS`**, `0.00%` errori.
- **Scaling HPA osservato su ML (`min=3`, `max=6`):**
  - Run `90s`, `-t4 -c150`: `1691.40 RPS`, `p99 149.65 ms`, `0.00%` errori; `diabetes-forecasting-hpa` scala da `3` a **`6` repliche** sotto picco CPU (`461%/70%` → `77%/70%` → `251%/70%`).
- **Percorso completo via Ingress (`POST https://localhost/api/patient/meals` con cookie HttpOnly, EdDSA, Redis TLS `mget`, ML HTTPS, PostgreSQL HA `verify-full`):**
  - Prima dell'ottimizzazione (`500m` CPU limit su `diabetes-pg`, 1 worker su `clinical`, PEM Ed25519 parsato ad ogni richiesta): saturazione CPU su `diabetes-pg-1` a `47.18 RPS` (`p99 1.89s`).
  - Dopo cache chiavi Ed25519 + `mget` Redis + `--workers 2` su `diabetes-clinical-api` + `1500m` CPU limit su `diabetes-pg`: **`103.24 RPS`** (obiettivo `>= 83.33`), **`p99 883.76 ms`** (`<= 1000 ms`), **`0.00%` errori** (`<= 0.50%`), con `diabetes-clinical-api` a `4` repliche.
- **Spiegazione differenza `~212 RPS` vs `~1674 RPS` nei vecchi report:** `~212 RPS` misurava il singolo tunnel SPDY di `kubectl port-forward` verso 1 solo pod con overhead client/port-forward locale, mentre `~1674 RPS` misura `wrk` in-cluster bilanciato da `kube-proxy` sulle 3 repliche.

## Scelte divergenti deliberate dal PDF

1. Il load balancer non interroga l'IAM ad ogni richiesta: il routing avviene per path nell'Ingress e la firma JWT viene verificata localmente nei servizi con scadenza breve e revoca JTI.
2. Non viene creato un secondo cluster PostgreSQL in KinD: la separazione è applicata tramite privilegi dedicati, viste e NetworkPolicy. Il PDF è da considerare una raffigurazione target per una produzione su larga scala.
3. Il training usa un modello pull verso la zona dedicata anziché un endpoint push di export.
4. L'ingestione CGM resta interna, secondo la decisione D2, con chiavi per dispositivo e port-forward solo per simulazione.
5. Componenti aggiuntivi come Redis Sentinel, cert-manager, service mesh e monitor esterno non vengono introdotti prima della verifica delle risorse disponibili sul portatile.
