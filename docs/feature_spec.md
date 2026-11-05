# Specifiche Tecniche del Contratto Dati (Feature Specification)

> **Nota sull'Assegnazione delle Tracce:**<br>
> La traccia ufficiale dell'esame prevede tre opzioni cliniche alternative (1. Diabete di Tipo 1, 2. Parkinson, 3. Alzheimer). 
> Il presente documento definisce formalmente il contratto dati adottato per la **prima fase di test architetturale**, basata sull'**Opzione 1 (Diabete di Tipo 1 - Time Series)**.
> Qualora al rilascio dei dataset venga assegnata l'Opzione 2 o l'Opzione 3, questo documento e lo schema Pydantic (`app/schemas.py`) verranno aggiornati con le feature tabulari o biomediche corrispondenti (es. frequenze vocali, tremori o tracciati clinici).

---

## 1. Struttura del Tensore di Input

Ogni richiesta di inferenza deve fornire una finestra temporale continua campionata su **12 intervalli consecutivi** (un intervallo ogni 5 minuti, corrispondente all'ultima ora di monitoraggio continuo):

$$\text{Shape del tensore di input:} \quad [1, 12, 3]$$

| Indice Canale | Nome Feature | Descrizione Clinica | Unità di Misura | Range Fisiologico Ammesso |
| :--- | :--- | :--- | :--- | :--- |
| **0** | `glucose_mg_dl` | Glicemia interstiziale (da CGM) | $\text{mg/dL}$ | $[20.0, 600.0]$ |
| **1** | `carbs_g` | Carboidrati assunti nel pasto | $\text{grammi}$ | $[0.0, 300.0]$ |
| **2** | `past_insulin_u`| Insulina attiva da boli precedenti (IOB) | $\text{Unità (IU)}$ | $[0.0, 100.0]$ |

---

## 2. Politica di Validazione e Tolleranza agli Errori

- **Dimensione fissa:** Vettori con lunghezza diversa da 12 timesteps o con numero di feature diverso da 3 vengono respinti con `HTTP 422 Unprocessable Entity`.
- **Valori Anomali / NaN / Inf:** Qualsiasi valore non numerico (`NaN`, `Infinity`, stringhe non convertibili) comporta l'immediato scarto della richiesta (`HTTP 422`).
- **Valori Fuori Scala:** Valori inferiori a $20\text{ mg/dL}$ o superiori a $600\text{ mg/dL}$ per la glicemia indicano un sensore scollegato o guasto e non vengono inoltrati alla rete neurale per prevenire stime pericolose.

---

## 3. Safety Guardrails sull'Output (Dose di Insulina)

La predizione finale $Y$ prodotta dal modello ONNX viene sottoposta a un vincolo matematico stringente prima di essere trasmessa all'attuatore/paziente:

$$\text{Dose Finale (IU)} = \min\left(\max(0.0, Y), \text{MAX\_INSULIN\_DOSE}\right)$$

- **Limite minimo:** $0.0\text{ IU}$ (l'insulina non può essere negativa).
- **Limite massimo (Clamp):** $20.0\text{ IU}$ (valore di sicurezza insuperabile configurabile da variabile d'ambiente `MAX_INSULIN_DOSE`).
- **Soglia di allerta (Warning):** Se la dose calcolata supera $15.0\text{ IU}$, la risposta include il flag `clinical_safety_status: "HIGH_DOSE_WARNING"`.
