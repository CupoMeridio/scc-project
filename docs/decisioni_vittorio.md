# Decisioni di Vittorio — 26 novembre 2026

Risposte alle domande della sezione 9 del report di Claude.

## D1. CSV OhioT1DM nel repo
**Decisione: NO.** I 24 CSV escono dal git. Il repo è pubblico e OhioT1DM è sotto accordo d'uso che vieta la ridistribuzione.
- Aggiungere `Diabetes/` al `.gitignore`.
- L'immagine seed legge i dati da un volume montato, non dall'immagine Docker.
- README documenta fonte e condizioni d'uso del dataset.

## D2. Endpoint `/api/cgm/ingest` esposto dall'Ingress
**Decisione: NO, solo interno.** Il simulatore CGM gira nel cluster o via port-forward. L'endpoint non è raggiungibile da Internet.

## D3. Restare su Ingress NGINX
**Decisione: Sì, con correzioni.** Resta per l'esame, ma:
- Rimuovere `server-snippet` e `configuration-snippet` (P3).
- `allow-snippet-annotations=false`, rimuovere `annotations-risk-level: Critical`.
- Aggiornare all'ultima release disponibile e fissarla.
- Documentare il ritiro del progetto nella relazione, citare Gateway API come percorso futuro.

## D4. Deploy su cluster acceso o destroy/recreate
**Decisione: Destroy/recreate sempre.** Nessun dato da preservare in sviluppo. Semplificare `deploy_k8s.sh` rimuovendo la logica di preservazione segreti (patch_if_missing, lettura password da DATABASE_URL esistente).

## D5. Script Windows (.ps1)
**Decisione: Sì, mantenere.** Il progetto deve essere testabile ed eseguibile ovunque. Entrambi `.sh` e `.ps1` restano.

## D6. Auth Bearer oltre al cookie
**Decisione: NO, rimuovere.** Il frontend usa cookie HttpOnly. Il Bearer si aggiunge quando serve un client non-browser (es. app mobile). Meno superficie d'attacco.

## D7. Dati CGM non recenti (P1): HTTP 409 o 200?
**Decisione: HTTP 200 con esito `NO_RECENT_DATA`.** Il paziente ottiene sempre una risposta comprensibile dal frontend, che mostra il messaggio neutro. Nessuna gestione errori separata nel JS.

## D8. Formato report Scout/wrk
**Decisione: Output grezzo in `docs/audit_reports/`.**
- `scout_<data>.txt` e `wrk_<data>.txt`.
- Niente post-processing, output verificabile tale quale.
- Includere comando, data, versione immagine, numero repliche e risorse pod.

---

# Decisioni di Vittorio — domande del report v2 (Q1–Q5)

Risposte alle domande della sezione 9 del «Report v2». Vincolanti come le precedenti.

## Q1. Campione reale del paziente 591
**Decisione: nessun dato reale nel repo.** Il file `Diabetes/sample_patient_591_testing.csv` non è più presente e non va reintrodotto. Se serve un campione per demo o test, si crea un file **sintetico** (valori inventati, dichiarati tali) in `tests/fixtures/`. Vale la D1.

## Q2. CSV già committati/pushati
**Decisione: sì, riscrivere la storia del repo pubblico.**
- I CSV sono nel commit `da7871a` e già pushati su `github.com/CupoMeridio/scc-project` (branch `main`).
- Passi: aggiornare `.gitignore` (`Diabetes/GlucoseClassification/`, `Diabetes/*.csv`), `git rm -r --cached Diabetes/GlucoseClassification`, riscrivere la storia con `git filter-repo --path Diabetes/GlucoseClassification --invert-paths`, quindi `git push --force` sul branch `main`.
- L'operazione è distruttiva sul repo pubblico: la esegue Vittorio.
- Verifica: `git log --all --oneline -- Diabetes/GlucoseClassification` non deve stampare nulla.

## Q3. Consegna delle chiavi dei dispositivi
**Decisione: Secret Kubernetes `device-keys` + comando CLI.**
- Il seed genera le chiavi con `secrets.token_urlsafe(32)`, salva in DB solo `sha256` e scrive il valore in chiaro, una volta, nel Secret `device-keys` (una voce per paziente). Il simulatore in-cluster lo legge da lì.
- In aggiunta, `python -m saas_app.device_cli create --patient <CODICE>` stampa la chiave una volta sola e `python -m saas_app.device_cli revoke --patient <CODICE>` imposta `revoked_at`.

## Q4. Versione di Ingress NGINX
**Decisione: fissare v1.15.1 (ultima release finale della community, 19 marzo 2026).**
- Repo `kubernetes/ingress-nginx` archiviato il 24 marzo 2026: nessun fix, nemmeno per CVE successive (es. CVE-2026-116472).
- Salvare il manifest in `k8s/vendor/ingress-nginx-v1.15.1.yaml` e pinnare il digest dell'immagine (ricavato con `docker buildx imagetools inspect`, non inventato).
- La relazione documenta il ritiro, il rischio delle CVE non patchate e Gateway API come percorso futuro (D3).

## Q5. Password temporanea del paziente
**Decisione: sì, generata dal server e mostrata una volta al medico.**
- `register_patient` non accetta più la password: il server ne genera una temporanea casuale, la restituisce **una sola volta** nella risposta di registrazione, imposta `must_change_password=True` e non la scrive mai nei log né nell'audit.

## Q6. Destino di `CgmReading.predicted_glucose_mmol` (P9)
**Decisione: eliminato dallo schema.** Era un valore inventato (scritto solo dal seed come `G(t+6) + bias`), senza un produttore reale: il modello produce solo la previsione a +30 min alla dichiarazione del pasto.
- Rimossa la colonna da `models.py` e dalla migrazione iniziale `001_initial_schema.py`, il campo dallo schema `CgmPoint`, le scritture dal seed e il dataset «Predetto +30m» dai grafici paziente/diabetologo.
- I grafici mostrano le previsioni **reali** a +30 min (`MealPrediction.forecasted_glucose_mmol`) posizionate all'istante di dichiarazione del pasto (RF10), tramite `alignMealForecasts` in `frontend/js/api.js`.
- La verità a terra (`MealPrediction.actual_glucose_30m_mmol`) è ora scritta dal worker a ogni lettura CGM (P9); MAE e bias restano nulli/«Dati insufficienti» finché non esiste almeno una coppia previsione/reale.
