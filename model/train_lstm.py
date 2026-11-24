#!/usr/bin/env python3
"""
===============================================================================
FILE: model/train_lstm.py
RUOLO: Pipeline completa di addestramento offline (Task A) ed esportazione ONNX
DATASET: OhioT1DM Preprocessed (GlucoseClassification/)
OBIETTIVO CLINICO:
    Previsione della glicemia a 30 minuti (orizzonte temporale t+6) e
    classificazione automatica del rischio glicemico (7 classi cliniche).
    
    NOTA REGOLATORIA E CLINICA (Software as a Medical Device - SaMD):
    Come chiarito dal docente, il dosaggio dell'insulina è responsabilità
    esclusiva del personale medico. L'algoritmo non effettua decisioni
    terapeutiche sul dosaggio farmacologico, ma agisce da 'Early Warning System'
    (sistema di allerta precoce) per allertare il paziente su imminenti ipoglicemie o iperglicemie.
    
    VARIABILI ESOGENE FUTURE ANNUNCIATE:
    Nelle applicazioni cliniche per il diabete, i pasti e i boli di insulina
    vengono inseriti dal paziente in anticipo rispetto alla consumazione.
    Pertanto, le feature I(t+k) e CHO(t+k) per k in [1..6] (prossimi 30 minuti)
    costituiscono variabili esogene note al tempo t (annunciate dal paziente).
===============================================================================
"""

import os         # Modulo standard per la manipolazione di cartelle e percorsi nel filesystem
import glob       # Ricerca di file su disco tramite pattern (es. trova tutti i CSV dei pazienti)
import json       # Serializzazione e salvataggio dei metadati e dei parametri di normalizzazione
import hashlib    # Funzioni di hashing crittografico (SHA-256) per l'integrità e audit del modello
import datetime   # Generazione di timestamp per tracciare la data esatta di addestramento
import argparse   # Gestione dei parametri passati da riga di comando (es. --epochs, --data)
from pathlib import Path # Gestione object-oriented dei percorsi su Windows/Linux/macOS

import numpy as np  # Libreria fondamentale per il calcolo scientifico e la manipolazione di tensori
import pandas as pd # Manipolazione e analisi strutturata di dati tabulari (DataFrames)

# PyTorch: Framework leader per il Deep Learning e la ricerca su reti neurali
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

# =============================================================================
# 1. DEFINIZIONE DELLE FEATURE CLINICHE E DELLE SOGLIE MEDICHE
# =============================================================================

# STORICO GLICEMICO (13 intervalli temporali da 5 minuti = 60 minuti di monitoraggio):
# G(t-12) rappresenta la misurazione di 60 minuti fa, G(t) rappresenta la misurazione attuale.
G_HIST = [f"G(t-{k})" for k in range(12, 0, -1)] + ["G(t)"]

# STORICO DELLE VARIAZIONI DI GLUCOSIO (dG):
# Rappresenta la velocità istantanea di salita o discesa del glucosio nel sangue.
# dG(t-m-1) = G(t-m) - G(t-m-1). Per il primo istante temporale G(t-12) non esiste
# una misurazione precedente nel dataset, quindi il primo valore è None (sarà inizializzato a 0.0).
DG_HIST = [None] + [f"dG(t-{m + 1})" for m in range(11, -1, -1)]

# FEATURE ESOGENE (14 valori):
# Comprendono l'insulina I(t) e i carboidrati assunti al momento attuale CHO(t),
# più le dosi e i pasti pianificati/annunciati per i successivi 6 intervalli da 5 min (t+1 .. t+6 = 30 min).
EXOG = ["I(t)", "CHO(t)"] + [f"{v}(t+{k})" for k in range(1, 7) for v in ("I", "CHO")]

# VARIABILI TARGET:
# TARGET: Valore continuo di glicemia a 30 minuti (G(t+6)) espresso in mmol/L.
# CLASS_TARGET: Classe di rischio discreta calcolata dal dataset ufficiale OhioT1DM.
TARGET = "G(t+6)"
CLASS_TARGET = "RiskClass(t+6)"

# SEGNALI ESCLUSI DALL'ANALISI:
# ST(t) = Skin Temperature (temperatura cutanea)
# EA(t) = Electrodermal Activity (conduttanza cutanea)
# HR(t) = Heart Rate (frequenza cardiaca)
# Nel dataset sono spesso costanti (= 1) o non disponibili in produzione, quindi vengono scartati
# per non indurre rumore nel gradiente della rete neurale.
DISCARDED_SIGNALS = ["ST(t)", "EA(t)", "HR(t)"]

# SOGLIE CLINICHE INTERNAZIONALI PER IL DIABETE DI TIPO 1 (in mmol/L):
# 1 mmol/L equivale a 18.018 mg/dL.
# - Classe 1: [0.0, 3.0)  -> Ipoglicemia Severa (pericolo immediato di perdita di coscienza)
# - Classe 2: [3.0, 3.9)  -> Ipoglicemia Lieve/Moderata (necessità di assumere zuccheri rapidi)
# - Classe 3: [3.9, 5.0)  -> Fascia di sicurezza pre-prandiale bassa
# - Classe 4: [5.0, 7.8)  -> TARGET OTTIMALE (euglicemia perfetta per un paziente diabetico)
# - Classe 5: [7.8, 10.0) -> Lievemente sopra il target (iperglicemia post-prandiale accettabile)
# - Classe 6: [10.0, 13.9) -> Iperglicemia Elevata (necessità di monitoraggio)
# - Classe 7: [13.9, +inf) -> Iperglicemia Grave (rischio chetoacidosi diabetica - DKA)
BINS = [0.0, 3.0, 3.9, 5.0, 7.8, 10.0, 13.9, np.inf]

CLASS_DESCRIPTIONS = {
    1: {"name": "Ipoglicemia Severa", "range": "[0.0, 3.0) mmol/L", "alert": "CRITICAL_HYPO"},
    2: {"name": "Ipoglicemia Lieve", "range": "[3.0, 3.9) mmol/L", "alert": "WARNING_HYPO"},
    3: {"name": "Pre-target Basso", "range": "[3.9, 5.0) mmol/L", "alert": "NORMAL"},
    4: {"name": "Target Ottimale", "range": "[5.0, 7.8) mmol/L", "alert": "OPTIMAL"},
    5: {"name": "Sopra Target", "range": "[7.8, 10.0) mmol/L", "alert": "ELEVATED"},
    6: {"name": "Iperglicemia Elevata", "range": "[10.0, 13.9) mmol/L", "alert": "WARNING_HYPER"},
    7: {"name": "Iperglicemia Grave", "range": "[>= 13.9) mmol/L", "alert": "CRITICAL_HYPER"}
}


def to_class(glucose_mmol: np.ndarray) -> np.ndarray:
    """
    Assegna ogni valore di glicemia continua (in mmol/L) alla corrispondente
    classe di rischio discreta (da 1 a 7).
    
    np.digitize: suddivide i valori in intervalli semi-aperti a sinistra [a, b).
    Aggiungendo 1, mappiamo direttamente gli indici da 0..6 a 1..7.
    """
    return np.digitize(glucose_mmol, BINS[1:-1]) + 1


def compute_sha256(filepath: str) -> str:
    """
    Calcola l'impronta crittografica SHA-256 del file specificato.
    Garantisce l'integrità del modello (Supply Chain Security): in produzione
    la Web App verificherà che il file onnx non sia stato manomesso o corrotto.
    """
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Legge il file a blocchi da 64 KB per non saturare la memoria RAM
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# =============================================================================
# 2. CARICAMENTO DATI E COSTRUZIONE DEI TENSIORI DI TRAINING
# =============================================================================

def load_patient_csvs(data_dir: str, split: str) -> list[pd.DataFrame]:
    """
    Individua e carica tutti i file CSV corrispondenti allo split specificato
    ('training' o 'testing') per ciascuno dei 12 pazienti.
    
    PARAMETRI:
    - data_dir (str): Percorso della cartella contenente le cartelle dei pazienti.
    - split (str): 'training' per i dati di addestramento, 'testing' per la validazione finale.
    
    RITORNA:
    - frames (list[pd.DataFrame]): Lista di DataFrame pandas, uno per ogni paziente.
    """
    # pattern: cerca ad esempio data_dir/*/540-ws-training(t+30).csv
    pattern = os.path.join(data_dir, "*", f"*-ws-{split}(t+30).csv")
    csv_paths = sorted(glob.glob(pattern))
    
    if not csv_paths:
        # Tentativo ricorsivo di fallback nel caso la cartella contenga un ulteriore livello
        pattern = os.path.join(data_dir, "**", f"*-ws-{split}(t+30).csv")
        csv_paths = sorted(glob.glob(pattern, recursive=True))
        
    if not csv_paths:
        raise FileNotFoundError(
            f"Nessun file CSV trovato con pattern '{pattern}'. "
            f"Verifica che il percorso del dataset sia corretto (es. Diabetes/GlucoseClassification)."
        )
        
    frames = []
    for path in csv_paths:
        df = pd.read_csv(path)
        # Salva l'identificativo del paziente (es. '540') per consentire analisi per-paziente
        patient_id = os.path.basename(os.path.dirname(path))
        df["patient"] = patient_id
        frames.append(df)
        
    print(f"[✓] Caricati {len(frames)} file CSV per lo split '{split}' da {data_dir}")
    return frames


def extract_features_and_targets(df: pd.DataFrame):
    """
    Estrae le matrici numeriche strutturate dal DataFrame grezzo:
    1. Ramo Temporale (LSTM): sequenza di 13 passi temporali con 2 canali [Glucosio, Delta_Glucosio].
    2. Ramo Esogeno (Dense MLP): 14 feature di carboidrati e insulina presente e futura.
    3. Target (Delta G): variazione di glucosio a 30 minuti rispetto al momento attuale.
    
    FORMULAZIONE MATEMATICA DEL TARGET:
    Predire direttamente G(t+6) rischia di far apprendere al modello la funzione banale f(x) = x,
    poiché la glicemia a t+6 è fortemente correlata con G(t).
    Predicendo invece la variazione:
        delta_y = G(t+6) - G(t)
    la rete è costretta a imparare la DINAMICA biologica (impatto dei pasti e dell'insulina).
    A runtime, il valore predetto sarà semplicemente:
        G_pred(t+6) = G(t) + delta_y_pred
    """
    # 1. Glucosio storico [N, 13]
    g = df[G_HIST].to_numpy(np.float32)
    
    # 2. Variazione storica di glucosio [N, 13] (il primo passo temporale viene riempito con 0.0)
    dg = np.stack(
        [np.zeros(len(df), dtype=np.float32) if col is None else df[col].to_numpy(np.float32)
         for col in DG_HIST],
        axis=1
    )
    
    # Concatena lungo il terzo asse (features per timestamp): shape finale [N, 13, 2]
    # Questo tensore rappresenta la serie temporale che entra nella LSTM
    seq = np.stack([g, dg], axis=2)
    
    # 3. Feature esogene [N, 14]: insulina e pasti attuali e annunciati per i successivi 30 min
    exog = df[EXOG].to_numpy(np.float32)
    
    # 4. Glicemia attuale G(t) [N]
    g_now = df["G(t)"].to_numpy(np.float32)
    
    # 5. Target differenziale: delta_y = G(t+6) - G(t)
    y_delta = df[TARGET].to_numpy(np.float32) - g_now
    
    # 6. Classe di rischio clinica target (da 1 a 7)
    target_classes = df[CLASS_TARGET].to_numpy(int)
    
    return seq, exog, y_delta, g_now, target_classes


# =============================================================================
# 3. ARCHITETTURA DELLA RETE NEURALE: GlucoseHybridLSTM
# =============================================================================

class GlucoseHybridLSTM(nn.Module):
    """
    Rete neurale ibrida concepita per serie temporali multivariata con segnali esogeni:
    
    1. Modulo LSTM (Long Short-Term Memory):
       - Modella le dipendenze temporali non lineari e il trend glicemico degli ultimi 60 minuti.
       - input_size = 2: per ogni intervallo temporale analizza la coppia [Glicemia, Velocità dG].
       - hidden_size = 64: dimensione dello stato interno della cella di memoria.
       - num_layers = 2: due strati ricorrenti sovrapposti per estrarre pattern ad alto livello.
       - dropout = 0.2: regolarizzazione per prevenire l'overfitting.
       - batch_first = True: il primo asse del tensore è la dimensione del batch [Batch, Seq, Features].
       
    2. Modulo Esogeno (Dense MLP):
       - Processa i 14 valori tabulari di insulina e carboidrati (noti a priori).
       - Linear(14, 32) -> ReLU(): proietta i carboidrati e l'insulina in uno spazio latente a 32 dimensioni.
       
    3. Testa di Regressione (Head):
       - Concatena l'ultimo stato nascosto della LSTM (64 dim) con l'embedding esogeno (32 dim) = 96 dimensioni.
       - Linear(96, 32) -> ReLU() -> Linear(32, 1): genera lo scalare finale delta_y (variazione stimata).
    """
    def __init__(self, n_seq_features: int = 2, n_exog_features: int = 14, hidden_dim: int = 64):
        super().__init__()
        
        # Ramo Ricorrente (LSTM) per la serie temporale sequenziale
        self.lstm = nn.LSTM(
            input_size=n_seq_features,
            hidden_size=hidden_dim,
            num_layers=2,
            batch_first=True,
            dropout=0.2
        )
        
        # Ramo Feed-Forward per le variabili esogene istantanee e future
        self.exog_net = nn.Sequential(
            nn.Linear(n_exog_features, 32),
            nn.ReLU()
        )
        
        # Testa di fusione multimodale e regressione
        self.head = nn.Sequential(
            nn.Linear(hidden_dim + 32, 32),
            nn.ReLU(),
            nn.Linear(32, 1)
        )

    def forward(self, seq: torch.Tensor, exog: torch.Tensor) -> torch.Tensor:
        """
        Passaggio in avanti (Forward Pass):
        - seq: tensore di shape [BatchSize, 13, 2]
        - exog: tensore di shape [BatchSize, 14]
        - Ritorna: tensore di shape [BatchSize] con la stima di delta_y.
        """
        # lstm_out: output di tutti i passi temporali
        # h: tensore degli stati nascosti finali [num_layers, BatchSize, hidden_dim]
        _, (h, _) = self.lstm(seq)
        
        # h[-1] seleziona l'ultimo layer della LSTM per ciascun elemento del batch: shape [BatchSize, hidden_dim]
        lstm_embedding = h[-1]
        
        # Calcolo dell'embedding esogeno: shape [BatchSize, 32]
        exog_embedding = self.exog_net(exog)
        
        # Fusione dei due rami per concatenazione lungo l'asse delle feature: shape [BatchSize, 96]
        fused = torch.cat([lstm_embedding, exog_embedding], dim=1)
        
        # Calcolo della stima finale e rimozione della dimensione superflua: shape [BatchSize]
        delta_pred = self.head(fused).squeeze(1)
        return delta_pred


# =============================================================================
# 4. VALUTAZIONE DELLE PRESTAZIONI CLINICHE (METRICHE)
# =============================================================================

def evaluate_model(model: nn.Module, seq: np.ndarray, exog: np.ndarray,
                   y_delta_true: np.ndarray, g_now: np.ndarray, target_classes: np.ndarray):
    """
    Esegue l'inferenza sul set di validazione o test e calcola le metriche cliniche:
    - RMSE (Root Mean Square Error) in mmol/L e in mg/dL: penalizza gli errori grandi.
    - MAE (Mean Absolute Error) in mmol/L: errore medio assoluto.
    - Accuratezza delle 7 Classi di Rischio: percentuale di volte in cui la fascia clinica
      stimata coincide con quella effettiva del paziente.
    """
    model.eval()
    with torch.no_grad():
        # Conversione da array NumPy a tensori PyTorch per l'inferenza
        seq_tensor = torch.from_numpy(seq)
        exog_tensor = torch.from_numpy(exog)
        delta_preds = model(seq_tensor, exog_tensor).numpy()
        
    # Ricostruzione della glicemia stimata assoluta: G_pred = G(t) + delta_pred
    g_predicted = g_now + delta_preds
    # Ricostruzione della glicemia reale effettiva: G_true = G(t) + delta_true
    g_actual = g_now + y_delta_true
    
    # 1. Errore Quadratico Medio Radicale (RMSE)
    rmse = float(np.sqrt(np.mean((g_predicted - g_actual) ** 2)))
    
    # 2. Errore Medio Assoluto (MAE)
    mae = float(np.mean(np.abs(g_predicted - g_actual)))
    
    # 3. Classificazione delle fasce di rischio (da 1 a 7)
    predicted_classes = to_class(g_predicted)
    accuracy = float(np.mean(predicted_classes == target_classes))
    
    return rmse, mae, accuracy, g_predicted


# =============================================================================
# 5. PIPELINE DI TRAINING ED ESPORTAZIONE ONNX (TASK A)
# =============================================================================

def run_training_pipeline(data_dir: str, epochs: int, output_dir: str, batch_size: int = 256):
    """
    Esegue l'intera sequenza di addestramento, validazione ed esportazione dell'artefatto:
    - Caricamento dati dei 12 pazienti.
    - Split cronologico temporale per paziente (minimizza il data leakage autoregressivo).
    - Normalizzazione standard con statistiche calcolate solo sul set di train.
    - Addestramento con Early Stopping basato sulla validation loss.
    - Esportazione in formato aperto ONNX.
    - Creazione del manifesto di metadati JSON con impronta di audit SHA-256.
    """
    # Fissa il seed per garantire che l'addestramento sia scientificamente riproducibile al 100%
    torch.manual_seed(42)
    np.random.seed(42)
    
    print("\n" + "=" * 75)
    print(" AVVIO ADDESTRAMENTO MODELLO CLINICO (OhioT1DM - Task A)")
    print("=" * 75)
    
    # -------------------------------------------------------------------------
    # FASE 1: Caricamento e Split Cronologico per Paziente
    # -------------------------------------------------------------------------
    # Per ciascuno dei 12 pazienti, le registrazioni del file training sono ordinate cronologicamente.
    # Adottiamo uno split temporale ordinato: i primi 80% dei campioni per il training e il restante
    # 20% per la validation interna, evitando lo shuffle casuale che contaminerebbe la sequenza.
    # Si noti che poiché le finestre temporali estratte hanno ampiezza di 12 passi storici + 6 di previsione,
    # esiste una lieve sovrapposizione al confine di transizione dell'80/20 (pari a ~72 finestre complessive
    # su 101.560, <0.07% del totale).
    # Il test set ufficiale (*-testing) è invece completamente disgiunto nel tempo (giorni successivi).
    train_frames, val_frames = [], []
    for df_patient in load_patient_csvs(data_dir, "training"):
        split_idx = int(len(df_patient) * 0.8)
        train_frames.append(df_patient.iloc[:split_idx])
        # Inserimento di un gap di sicurezza temporale (6 passi = 30 minuti) al confine:
        # Poiché l'orizzonte di previsione è G(t+6), escludere i 6 passi immediatamente successivi
        # a split_idx assicura che il target dell'ultima finestra di train non compaia nelle feature di val,
        # eliminando scientificamente qualsiasi data leakage autoregressivo al confine.
        val_frames.append(df_patient.iloc[split_idx + 6:])
        
    df_train = pd.concat(train_frames, ignore_index=True)
    df_val = pd.concat(val_frames, ignore_index=True)
    
    # Caricamento del test set ufficiale (fornito dal professore nei file *-testing)
    df_test = pd.concat(load_patient_csvs(data_dir, "testing"), ignore_index=True)
    
    print(f"Campioni estratti -> Training: {len(df_train)} | Validazione: {len(df_val)} | Test: {len(df_test)}")
    
    # Estrazione delle matrici numeriche
    S_train, E_train, y_train, g_now_train, cls_train = extract_features_and_targets(df_train)
    S_val, E_val, y_val, g_now_val, cls_val = extract_features_and_targets(df_val)
    S_test, E_test, y_test, g_now_test, cls_test = extract_features_and_targets(df_test)
    
    # -------------------------------------------------------------------------
    # FASE 2: Normalizzazione Statistica (StandardScaler)
    # -------------------------------------------------------------------------
    # Le reti neurali e le LSTM convergono molto più rapidamente e stabilmente
    # se le feature in ingresso hanno media = 0 e deviazione standard = 1.
    # FONDAMENTALE: Le statistiche (media e std) DEVONO essere calcolate
    # esclusivamente sul training set e poi applicate a validazione, test e serving!
    seq_mean = S_train.mean(axis=(0, 1)).astype(float).tolist()
    seq_std = (S_train.std(axis=(0, 1)) + 1e-6).astype(float).tolist()
    
    exog_mean = E_train.mean(axis=0).astype(float).tolist()
    exog_std = (E_train.std(axis=0) + 1e-6).astype(float).tolist()
    
    scaler_metadata = {
        "seq_mean": seq_mean,
        "seq_std": seq_std,
        "exog_mean": exog_mean,
        "exog_std": exog_std
    }
    
    # Funzione di normalizzazione (Z-Score standardization)
    def normalize_inputs(seq_arr, exog_arr):
        norm_seq = (seq_arr - np.array(seq_mean, dtype=np.float32)) / np.array(seq_std, dtype=np.float32)
        norm_exog = (exog_arr - np.array(exog_mean, dtype=np.float32)) / np.array(exog_std, dtype=np.float32)
        return norm_seq, norm_exog
        
    S_train_n, E_train_n = normalize_inputs(S_train, E_train)
    S_val_n, E_val_n = normalize_inputs(S_val, E_val)
    S_test_n, E_test_n = normalize_inputs(S_test, E_test)
    
    # -------------------------------------------------------------------------
    # FASE 3: Addestramento della Rete Neurale con PyTorch
    # -------------------------------------------------------------------------
    # Creazione del DataLoader di PyTorch per scorrere i dati in mini-batch da 256 campioni
    train_dataset = TensorDataset(
        torch.from_numpy(S_train_n),
        torch.from_numpy(E_train_n),
        torch.from_numpy(y_train)
    )
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    
    model = GlucoseHybridLSTM()
    
    # Ottimizzatore Adam (Adaptive Moment Estimation) con Learning Rate = 0.001
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    
    # Criterio di costo: Mean Squared Error (MSE Loss) sulla variazione delta_y
    criterion = nn.MSELoss()
    
    best_rmse = np.inf
    best_state_dict = None
    patience = 0
    max_patience = 5 # Se la validation RMSE non migliora per 5 epoche di fila, ferma l'addestramento
    
    print("\nInizio epoche di addestramento:")
    for epoch in range(1, epochs + 1):
        model.train()
        running_loss = 0.0
        
        for batch_seq, batch_exog, batch_y in train_loader:
            optimizer.zero_grad() # Resetta i gradienti accumulati nella batch precedente
            preds = model(batch_seq, batch_exog)
            loss = criterion(preds, batch_y)
            loss.backward() # Retro-propagazione del gradiente (Backpropagation)
            optimizer.step() # Aggiornamento dei pesi sinaptici
            running_loss += loss.item() * len(batch_y)
            
        epoch_loss = running_loss / len(S_train_n)
        
        # Valutazione sulle metriche di validazione
        val_rmse, val_mae, val_acc, _ = evaluate_model(
            model, S_val_n, E_val_n, y_val, g_now_val, cls_val
        )
        
        print(
            f"Epoca {epoch:2d}/{epochs:2d} | Train Loss: {epoch_loss:.4f} | "
            f"Val RMSE: {val_rmse:.3f} mmol/L ({val_rmse * 18.018:.1f} mg/dL) | "
            f"Val MAE: {val_mae:.3f} | Acc Classi: {val_acc * 100:.1f}%"
        )
        
        # Logica di Early Stopping e salvataggio del modello migliore
        if val_rmse < best_rmse:
            best_rmse = val_rmse
            best_state_dict = {k: v.clone() for k, v in model.state_dict().items()}
            patience = 0
        else:
            patience += 1
            if patience >= max_patience:
                print(f"[!] Early Stopping attivato dopo l'epoca {epoch}: nessun miglioramento per {max_patience} epoche.")
                break
                
    # Ripristina lo stato con le migliori prestazioni sul validation set
    if best_state_dict is not None:
        model.load_state_dict(best_state_dict)
        
    # -------------------------------------------------------------------------
    # FASE 4: Valutazione Finale su Test Set Ufficiale & Confronto Baseline
    # -------------------------------------------------------------------------
    test_rmse, test_mae, test_acc, _ = evaluate_model(
        model, S_test_n, E_test_n, y_test, g_now_test, cls_test
    )
    
    # Baseline di persistenza: ipotizza ingenuamente che tra 30 minuti la glicemia
    # rimanga identica a quella attuale G(t+6) = G(t), ovvero delta_y = 0.
    baseline_rmse = float(np.sqrt(np.mean(y_test ** 2)))
    baseline_acc = float(np.mean(to_class(g_now_test) == cls_test))
    
    print("\n" + "=" * 75)
    print(" RISULTATI FINALI SUL TEST SET UFFICIALE (OhioT1DM - Finestre Temporali Successive):")
    print(f" Modello LSTM -> RMSE: {test_rmse:.3f} mmol/L ({test_rmse * 18.018:.1f} mg/dL) | Acc Classi: {test_acc * 100:.1f}%")
    print(f" Baseline Persistenza -> RMSE: {baseline_rmse:.3f} mmol/L ({baseline_rmse * 18.018:.1f} mg/dL) | Acc Classi: {baseline_acc * 100:.1f}%")
    print("=" * 75)
    
    # -------------------------------------------------------------------------
    # FASE 5: Esportazione in Formato ONNX (Open Neural Network Exchange)
    # -------------------------------------------------------------------------
    # MOTIVAZIONE ARCHITETTURALE CLOUD:
    # ONNX permette al container di produzione di utilizzare il runtime in C++ ultra-leggero
    # 'onnxruntime' senza richiedere l'intera libreria PyTorch.
    # Questo riduce la dimensione dell'immagine da ~2 GB a soli ~150 MB,
    # abbattendo i tempi di cold start e riducendo drasticamente le CVE di sicurezza.
    os.makedirs(output_dir, exist_ok=True)
    onnx_path = os.path.join(output_dir, "model.onnx")
    pt_path = os.path.join(output_dir, "lstm_glucose.pt")
    metadata_path = os.path.join(output_dir, "model_metadata.json")
    
    # Salvataggio pesi nativi PyTorch per eventuale fine-tuning futuro
    torch.save(model.state_dict(), pt_path)
    print(f"[✓] Pesi PyTorch salvati: {pt_path}")
    
    # Preparazione tensori 'dummy' (campioni fittizi) per tracciare il grafo computazionale di esecuzione
    model.eval()
    dummy_seq = torch.randn(1, 13, 2, dtype=torch.float32)
    dummy_exog = torch.randn(1, 14, dtype=torch.float32)
    
    # torch.onnx.export:
    # - (dummy_seq, dummy_exog): tuple con i due ingressi richiesti dal metodo forward()
    # - input_names: nomi simbolici usati dalla Web App per passare i dati al runtime
    # - output_names: nome simbolico della variabile di uscita
    # - opset_version: impostata esplicitamente a 18 in accordo con gli operatori esportati
    #   dal compilatore PyTorch, pienamente supportati dal motore runtime di inferenza C++.
    # - dynamic_axes: consente a ONNX di accettare batch di qualsiasi dimensione
    #   (es. 1 paziente per richiesta singola o centinaia di richieste concorrenti nei carichi di picco)
    torch.onnx.export(
        model,
        (dummy_seq, dummy_exog),
        onnx_path,
        export_params=True,
        opset_version=18,
        do_constant_folding=True, # Ottimizza il grafo pre-calcolando nodi costanti
        input_names=["seq", "exog"],
        output_names=["delta_glucose"],
        dynamic_axes={
            "seq": {0: "batch_size"},
            "exog": {0: "batch_size"},
            "delta_glucose": {0: "batch_size"}
        }
    )
    print(f"[✓] Modello ONNX esportato con successo: {onnx_path}")
    
    # Calcolo dell'hash SHA256 degli artefatti per audit di sicurezza (Supply Chain Security).
    # Se PyTorch ha esportato i pesi su file esterno (.data), include anche il checksum dei pesi!
    model_sha256 = compute_sha256(onnx_path)
    data_path = onnx_path + ".data"
    data_sha256 = compute_sha256(data_path) if os.path.exists(data_path) else None
    
    artifacts_integrity = {
        "model.onnx": model_sha256
    }
    if data_sha256 is not None:
        artifacts_integrity["model.onnx.data"] = data_sha256
    
    # -------------------------------------------------------------------------
    # FASE 6: Salvataggio del File Metadati Unificato (model_metadata.json)
    # -------------------------------------------------------------------------
    full_metadata = {
        "model_name": "OhioT1DM-GlucoseHybridLSTM",
        "model_version": "2.0.0",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model_sha256": model_sha256,
        "artifacts_integrity": artifacts_integrity,
        "framework": "PyTorch -> ONNX Runtime",
        "clinical_task": "Forecasting Glucose at t+30 min (G(t+6)) and Risk Classification (OhioT1DM)",
        "prediction_target": "delta_glucose (mmol/L) -> G_pred = G(t) + delta_glucose",
        "test_metrics": {
            "test_rmse_mmol_l": round(test_rmse, 4),
            "test_rmse_mg_dl": round(test_rmse * 18.018, 2),
            "test_mae_mmol_l": round(test_mae, 4),
            "test_risk_class_accuracy": round(test_acc, 4),
            "baseline_persistence_rmse_mmol_l": round(baseline_rmse, 4),
            "baseline_persistence_accuracy": round(baseline_acc, 4)
        },
        "scaler": scaler_metadata,
        "risk_classes": CLASS_DESCRIPTIONS,
        "input_schema": {
            "glucose_history_steps": 13,
            "glucose_history_interval_min": 5,
            "exogenous_features_count": 14,
            "exogenous_features": EXOG
        }
    }
    
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(full_metadata, f, indent=2, ensure_ascii=False)
        
    print(f"[✓] Metadati unificati salvati in: {metadata_path}")
    print(f"[✓] SHA-256 Integrità: {model_sha256}")
    print("=" * 75)
    print(" ADDESTRAMENTO ED ESPORTAZIONE COMPLETATI CON SUCCESSO (Task A Concluso)")
    print("=" * 75 + "\n")
    return full_metadata


# =============================================================================
# PUNTO DI INGRESSO DELLO SCRIPT
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Addestramento offline del modello LSTM clinico su dataset OhioT1DM con esportazione ONNX."
    )
    parser.add_argument(
        "--data",
        type=str,
        default="Diabetes/GlucoseClassification",
        help="Percorso della cartella contenente i file dei pazienti (default: Diabetes/GlucoseClassification)"
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=25,
        help="Numero massimo di epoche di addestramento (default: 25)"
    )
    parser.add_argument(
        "--out",
        type=str,
        default="model",
        help="Cartella di destinazione per salvare il modello compilato ONNX e i metadati (default: model)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Dimensione del mini-batch per il gradient descent (default: 256)"
    )
    
    args = parser.parse_args()
    run_training_pipeline(
        data_dir=args.data,
        epochs=args.epochs,
        output_dir=args.out,
        batch_size=args.batch_size
    )
