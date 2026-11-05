#!/usr/bin/env python3
"""
FILE: model/train_mock_model.py
RUOLO: Script di addestramento offline ed esportazione del modello in formato ONNX.
"""

import os        # Modulo per interagire con il file system (creare cartelle, verificare percorsi)
import json      # Modulo per serializzare e deserializzare dati in formato JSON
import hashlib   # Modulo crittografico per calcolare hash di integrità (SHA256)
import datetime  # Modulo per gestire date e orari (timestamp di creazione modello)
import argparse  # Modulo per gestire argomenti passati da riga di comando (es. --epochs 25)
import numpy as np # Libreria standard per calcolo numerico e manipolazione di matrici/tensori
from numpy.typing import NDArray

def generate_synthetic_diabetes_data(
    num_samples: int = 2000,
    seq_len: int = 12,
    seed: int = 42,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """
    Genera campioni artificiali realistici di serie temporali per simulare i pazienti.
    
    PARAMETRI:
    - num_samples (int): Numero totale di finestre temporali da generare (default: 2000).
    - seq_len (int): Lunghezza della sequenza temporale (default: 12 intervalli da 5 min = 1 ora).
    - seed (int): Valore per inizializzare il generatore pseudocasuale. Fissare il seed (42)
      garantisce che chiunque esegua il codice ottenga ESATTAMENTE gli stessi numeri (Riproducibilità scientifica).
    
    RITORNA:
    - X (np.ndarray): Tensore delle feature di shape [2000, 12, 3] (3 canali per ogni istante).
    - y (np.ndarray): Vettore del target di shape [2000, 1] (dose di insulina calcolata).
    """
    # Fissa il generatore casuale di NumPy per avere dati riproducibili ad ogni esecuzione
    np.random.seed(seed)
    
    # -------------------------------------------------------------------------
    # CANALE 0: Glicemia (mg/dL)
    # np.random.normal(loc=130, scale=30, size=...):
    #   - loc=130: media della distribuzione (130 mg/dL è un valore plausibile per un diabetico)
    #   - scale=30: deviazione standard (la maggior parte dei valori oscillerà tra 100 e 160)
    #   - size=(num_samples, seq_len, 1): genera una matrice 3D [2000, 12, 1]
    # .clip(70.0, 350.0):
    #   - taglia forzatamente i valori al di sotto di 70 (ipoglicemia) o al di sopra di 350 (iperglicemia grave)
    # -------------------------------------------------------------------------
    bg = np.random.normal(130, 30, (num_samples, seq_len, 1)).clip(70.0, 350.0)
    
    # -------------------------------------------------------------------------
    # CANALE 1: Carboidrati assunti (grammi)
    # np.random.exponential(scale=15, size=...):
    #   - usa la distribuzione esponenziale perché i pasti sono eventi sporadici:
    #     molti intervalli avranno 0g di carboidrati, mentre occasionalmente ci saranno picchi (es. 40-70g).
    # .clip(0.0, 100.0):
    #   - impedisce valori negativi e pone un tetto massimo a 100g di carboidrati per singolo pasto.
    # -------------------------------------------------------------------------
    carbs = np.random.exponential(15, (num_samples, seq_len, 1)).clip(0.0, 100.0)
    
    # -------------------------------------------------------------------------
    # CANALE 2: Insulina attiva precedente (Unità - IU)
    # Rappresenta l'insulina iniettata nelle ore precedenti che sta ancora agendo nel sangue (Insulin On Board - IOB).
    # .clip(0.0, 15.0): taglia i valori tra 0 e 15 Unità.
    # -------------------------------------------------------------------------
    past_insulin = np.random.exponential(1.5, (num_samples, seq_len, 1)).clip(0.0, 15.0)
    
    # -------------------------------------------------------------------------
    # UNIONE DELLE FEATURE LUNGO L'ASSE DEI CANALI (axis=-1, cioè l'ultimo asse):
    # Concatena i tre tensori [2000, 12, 1] trasformandoli in un unico tensore [2000, 12, 3].
    # .astype(np.float32): converte in virgola mobile a 32 bit (obbligatorio per PyTorch e ONNX,
    # poiché i 64 bit raddoppiano inutilmente la RAM usata).
    # -------------------------------------------------------------------------
    X = np.concatenate([bg, carbs, past_insulin], axis=-1).astype(np.float32)
    
    # Estrazione dell'ultimo istante temporale (-1) per il calcolo del bolo necessario al momento attuale:
    recent_carbs = carbs[:, -1, 0]        # Ultimo valore dei carboidrati
    recent_bg = bg[:, -1, 0]              # Ultimo valore della glicemia misurata
    active_ins = past_insulin[:, -1, 0]   # Insulina ancora attiva nel corpo
    
    # Formula clinica standard per la stima del bolo insulinico:
    # 1. Componente carboidrati: carboidrati / ICR (Insulin to Carb Ratio, standard 10g per 1 IU)
    # 2. Componente correttiva glicemica: max(0, (Glicemia - Target 100) / Fattore Sensibilità ISF 40)
    # 3. Compensazione insulina attiva (IOB): sottrazione dell'insulina ancora in circolo per prevenire sovra-dosaggi
    y = (recent_carbs / 10.0) + np.maximum(0.0, (recent_bg - 100.0) / 40.0) - (active_ins * 0.5)
    
    # Applicazione di rumore gaussiano (media 0, deviazione 0.1) per simulare la variabilità biologica,
    # con delimitazione del target nell'intervallo [0, 20] Unità per vincoli di sicurezza clinica.
    # .reshape(-1, 1): trasforma il vettore in matrice colonna [2000, 1]
    y = np.clip(y + np.random.normal(0, 0.1, num_samples), 0.0, 20.0).astype(np.float32).reshape(-1, 1)
    
    return X, y

def compute_sha256(filepath: str) -> str:
    """
    Calcola l'impronta digitale crittografica SHA256 del file specificato.
    
    PARAMETRI:
    - filepath (str): Percorso del file su file system di cui calcolare l'impronta.
    
    VALORE DI RITORNO:
    - str: Stringa esadecimale a 64 caratteri che identifica univocamente l'integrità del file.
    
    DETTAGLI DI SINTASSI:
    - 'open(filepath, "rb")': apre il file in modalità binaria grezza (read-binary) per elaborare qualsiasi formato.
    - 'while chunk := f.read(8192):': costrutto Walrus Operator (':=') introdotto in Python 3.8.
      Assegna alla variabile 'chunk' un blocco di 8192 byte (8 KB) e ne valuta contestualmente la condizione:
      finché sono presenti byte non letti, il blocco viene passato a 'h.update(chunk)'; quando il file termina,
      'f.read()' restituisce un byte-array vuoto (b"") che interrompe il ciclo senza saturare la RAM.
    """
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def train_and_export(
    output_model_path: str = "model/model.onnx",
    output_metadata_path: str = "model/model_metadata.json",
    epochs: int = 25,
) -> dict[str, object]:
    """
    Pipeline di training supervisionato, valutazione analitica ed esportazione in formato standard ONNX.
    
    PARAMETRI:
    - output_model_path (str): Percorso di destinazione del grafo computazionale compilato (default: "model/model.onnx").
    - output_metadata_path (str): Percorso del file JSON contenente i metadati di audit (default: "model/model_metadata.json").
    - epochs (int): Numero di epoche di ottimizzazione dei parametri con algoritmo Adam (default: 25).
    
    VALORE DI RITORNO:
    - dict[str, object]: Dizionario contenente i metadati completi del modello, le metriche calcolate (MSE e MAE)
      e l'impronta crittografica SHA256 dell'artefatto salvato.
    """
    # Crea la directory di destinazione se non esiste già (evita errori di 'Directory not found')
    os.makedirs(os.path.dirname(output_model_path), exist_ok=True)

    # Generazione di 2500 campioni suddivisi in:
    # - 2000 campioni di Training (per l'ottimizzazione dei pesi della rete)
    # - 500 campioni di Validazione (per calcolare metriche reali e verificare l'assenza di overfitting)
    X, y = generate_synthetic_diabetes_data(num_samples=2500, seq_len=12, seed=42)
    # I primi 2000 campioni aggiornano i pesi; gli ultimi 500 restano fuori
    # dall'ottimizzazione e servono a misurare l'errore su dati non usati
    # per addestrare. Essendo dati sintetici, queste metriche non certificano
    # le prestazioni cliniche su pazienti reali.
    train_size = 2000
    X_train_np, y_train_np = X[:train_size], y[:train_size]
    X_val_np, y_val_np = X[train_size:], y[train_size:]
    
    try:
        import torch
        import torch.nn as nn
        # Imposta il seed del generatore casuale di PyTorch: rende ripetibili
        # l'inizializzazione iniziale dei pesi e gli altri calcoli casuali.
        torch.manual_seed(42)
        print("[+] PyTorch rilevato: costruzione del modello sequenziale LSTM...")
        
        # ---------------------------------------------------------------------
        # DEFINIZIONE DELL'ARCHITETTURA NEURALE
        # ---------------------------------------------------------------------
        # nn.Module è la classe base di PyTorch per costruire reti neurali.
        # Ereditandola, PyTorch può registrare i livelli e i relativi pesi,
        # calcolare i gradienti e applicare le modalità train/eval al modello.
        class DiabetesLSTM(nn.Module):
            """
            Architettura neurale ricorrente basata su cella LSTM accoppiata a un regressore denso (MLP).
            Progettata specificamente per l'elaborazione di finestre temporali di serie storiche mediche [12 x 3].
            """
            def __init__(
                self,
                input_size: int = 3,
                hidden_size: int = 16,
                num_layers: int = 1,
            ) -> None:
                """
                Inizializza i pesi e i livelli della rete neurale.
                
                PARAMETRI:
                - input_size (int): Numero di canali informativi per ciascun intervallo temporale (default: 3,
                  corrispondenti a glicemia mg/dL, carboidrati g, insulina attiva U).
                - hidden_size (int): Dimensione dello spazio latente dello stato interno della cella LSTM (default: 16).
                  Determina la capacità di memoria a lungo e breve termine della rete.
                - num_layers (int): Numero di livelli ricorrenti LSTM sovrapposti verticalmente (default: 1).
                """
                super().__init__()

                # LSTM (Long Short-Term Memory) elabora la sequenza temporale.
                # batch_first=True impone il formato dei tensori [batch_size, seq_len, num_features].
                self.lstm = nn.LSTM(
                    input_size=input_size,
                    hidden_size=hidden_size,
                    num_layers=num_layers,
                    batch_first=True
                )

                # Regressore fully-connected a due stadi con attivazione ReLU non lineare.
                # Linear(16 -> 8) comprime la rappresentazione latente;
                # Linear(8 -> 1) calcola la stima scalare della dose in Unità (IU);
                # L'ultima ReLU garantisce l'assenza di dosi negative in uscita.
                self.regressor = nn.Sequential(
                    nn.Linear(hidden_size, 8),
                    nn.ReLU(),
                    nn.Linear(8, 1),
                    nn.ReLU()
                )

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                """
                Esegue il passaggio in avanti (Forward Pass) attraverso il grafo neurale.
                
                PARAMETRI:
                - x (torch.Tensor): Tensore di input di forma [batch_size, 12, 3].
                
                VALORE DI RITORNO:
                - torch.Tensor: Tensore contenente la dose di insulina prevista di forma [batch_size, 1].
                """
                # lstm_out contiene le uscite per tutti i 12 timesteps con forma [batch, 12, hidden_size]
                lstm_out, _ = self.lstm(x)

                # Slicing: estrae la rappresentazione interna all'ultimo intervallo temporale (-1)
                last_step = lstm_out[:, -1, :]

                # Calcola la stima della dose finale
                pred = self.regressor(last_step)
                return pred

        # Crea il modello con i valori predefiniti sopra specificati.
        model = DiabetesLSTM()

        # Adam aggiorna i pesi usando i gradienti calcolati durante il training.
        # lr (learning rate) controlla l'ampiezza di ciascun aggiornamento.
        optimizer = torch.optim.Adam(model.parameters(), lr=0.01)

        # La Mean Squared Error misura la media dei quadrati delle differenze
        # tra dose prevista e dose di riferimento.
        criterion = nn.MSELoss()
        
        # PyTorch opera su tensori: from_numpy converte gli array NumPy in
        # tensori e, quando possibile, condivide la stessa memoria.
        # X contiene gli input; y contiene le dosi di riferimento. Le varianti
        # train servono ad aggiornare i pesi, quelle val solo alla valutazione.
        X_train, y_train = torch.from_numpy(X_train_np), torch.from_numpy(y_train_np)
        X_val, y_val = torch.from_numpy(X_val_np), torch.from_numpy(y_val_np)
        
        # ---------------------------------------------------------------------
        # LOOP DI ADDESTRAMENTO OFFLINE (Epoche)
        # ---------------------------------------------------------------------
        # train() attiva la modalità di addestramento. Un'epoca è un passaggio
        # completo sui campioni di training; qui ogni iterazione usa l'intero
        # gruppo X_train (non viene suddiviso in mini-batch).
        model.train()
        for epoch in range(epochs):
            # Azzera i gradienti del passo precedente, altrimenti PyTorch li
            # accumulerebbe.
            optimizer.zero_grad()

            # Passaggio in avanti: calcola le dosi previste dal modello.
            preds = model(X_train)

            # Confronta le previsioni con le dosi note e calcola l'errore.
            loss = criterion(preds, y_train)

            # Passaggio all'indietro: calcola come ciascun peso ha contribuito
            # all'errore e memorizza i gradienti sui parametri del modello.
            loss.backward()

            # Aggiorna i pesi usando i gradienti e il learning rate di Adam.
            optimizer.step()
        
        # ---------------------------------------------------------------------
        # VALUTAZIONE DELLE METRICHE CLINICHE
        # ---------------------------------------------------------------------
        # eval() mette il modello in modalità di valutazione. no_grad() evita
        # di calcolare gradienti, non necessari perché qui i pesi non cambiano.
        model.eval()
        with torch.no_grad():
            # Previsioni sui 500 campioni tenuti separati dal training.
            val_preds = model(X_val)

            # item() estrae il singolo valore dal tensore; float() lo converte
            # in un numero Python serializzabile nei metadati JSON.
            val_mse = float(criterion(val_preds, y_val).item())
            val_mae = float(torch.mean(torch.abs(val_preds - y_val)).item())
            
        print(f"[+] Training terminato. Metriche Validazione Reali: MSE={val_mse:.4f}, MAE={val_mae:.4f} IU")
        
        # ---------------------------------------------------------------------
        # ESPORTAZIONE IN FORMATO ONNX (Open Neural Network Exchange)
        # ---------------------------------------------------------------------
        # L'esportatore esegue il modello con un input di esempio per ricostruire
        # il grafo. Forma [1, 12, 3]: un campione, 12 intervalli, 3 feature;
        # float32 corrisponde al formato usato nei dati generati.
        dummy_input = torch.randn(1, 12, 3, dtype=torch.float32)
        
        torch.onnx.export(
            model,
            dummy_input,
            output_model_path,
            export_params=True,  # Include nel file ONNX i pesi appresi dal modello.
            opset_version=17,  # Versione delle operazioni ONNX usate per serializzare il grafo.
            input_names=['input_time_series'],  # Nome leggibile dell'ingresso del modello.
            output_names=['forecasted_insulin'],  # Nome leggibile dell'uscita prevista.
            dynamic_axes={
                # L'asse 0 è la dimensione del batch: può variare in inferenza
                # senza dover esportare un modello diverso per ogni batch.
                'input_time_series': {0: 'batch_size'},
                'forecasted_insulin': {0: 'batch_size'}
            }
        )

        # Nome descrittivo incluso nei metadati generati più avanti.
        architecture_name = "LSTM(input=3, hidden=16, layers=1) -> Linear(8) -> Linear(1)"

    except ImportError:
        # Questo blocco scatta se PyTorch non è installato sul sistema host:
        # Genera un grafo ONNX valido direttamente con la libreria 'onnx', senza richiedere 800MB di PyTorch
        print("[!] PyTorch non trovato. Generazione grafo minimale con libreria ONNX...")
        import onnx
        from onnx import helper, TensorProto
        
        # Grafo di riserva più semplice della rete LSTM: usa solo le tre feature
        # dell'ultimo intervallo. Flatten trasforma ogni input [12, 3] in 36
        # valori ordinati per intervallo, mantenendo separato l'asse batch.
        node_flatten = helper.make_node('Flatten', inputs=['input_time_series'], outputs=['flat_input'], axis=1)
        
        # Gemm calcolerà un'uscita affine: input @ weights + bias.
        # Gli indici 33, 34 e 35 corrispondono alle tre feature dell'ultimo
        # intervallo: glicemia, carboidrati e insulina attiva. Gli altri pesi
        # restano a zero e quindi gli intervalli precedenti non contribuiscono.
        # I coefficienti implementano carboidrati/10 + glicemia/40
        # - insulina_attiva*0.5 - 100/40; il termine -100/40 nel bias sposta
        # la correzione rispetto al target glicemico di 100 mg/dL.
        weights = np.zeros((36, 1), dtype=np.float32)
        weights[33, 0] = 1.0 / 40.0   # Fattore di sensibilità alla glicemia (ISF: 1 IU ogni 40 mg/dL)
        weights[34, 0] = 1.0 / 10.0   # Rapporto insulina/carboidrati (ICR: 1 IU ogni 10g)
        weights[35, 0] = -0.5          # Tasso di assorbimento dell'insulina attiva (IOB)
        bias = np.array([-2.5], dtype=np.float32) # Offset correttivo rispetto al target glicemico basale (-100/40)
        
        # ONNX memorizza i parametri come tensori protobuf: W ha forma [36, 1]
        # (36 ingressi e una dose in uscita), B contiene il singolo bias.
        w_tensor = helper.make_tensor('W', TensorProto.FLOAT, [36, 1], weights.flatten().tolist())
        b_tensor = helper.make_tensor('B', TensorProto.FLOAT, [1], bias.flatten().tolist())
        
        # Gemm applica la trasformazione lineare con i pesi e il bias.
        node_gemm = helper.make_node('Gemm', inputs=['flat_input', 'W', 'B'], outputs=['gemm_out'], alpha=1.0, beta=1.0)
        # ReLU tronca a zero le dosi negative. Questo fallback non applica qui
        # un limite superiore; l'API gestisce separatamente i propri guardrail.
        node_relu = helper.make_node('Relu', inputs=['gemm_out'], outputs=['forecasted_insulin'])
        
        # Dichiarazioni delle porte del grafo: batch_size è simbolico e può
        # variare; ciascun campione deve avere 12 intervalli e 3 feature,
        # mentre il modello produce una dose (forma [batch, 1]).
        input_val = helper.make_tensor_value_info('input_time_series', TensorProto.FLOAT, ['batch_size', 12, 3])
        output_val = helper.make_tensor_value_info('forecasted_insulin', TensorProto.FLOAT, ['batch_size', 1])
        
        # Collega i nodi nell'ordine Flatten -> Gemm -> ReLU e include W e B
        # come initializer: sono i parametri costanti del modello, non input API.
        graph = helper.make_graph(
            nodes=[node_flatten, node_gemm, node_relu],
            name='DiabetesForecastGraph',
            inputs=[input_val],
            outputs=[output_val],
            initializer=[w_tensor, b_tensor]
        )
        
        # Crea il modello ONNX dal grafo e lo salva nel percorso richiesto.
        model_def = helper.make_model(graph, producer_name='scc-diabetes-baseline')
        onnx.save(model_def, output_model_path)
        
        # Calcola le stesse metriche sul set di validazione usando NumPy,
        # applicando ai campioni lo stesso grafo lineare e la ReLU.
        flat_val = X_val_np.reshape(X_val_np.shape[0], -1)
        val_preds_np = np.maximum(0.0, np.dot(flat_val, weights) + bias).flatten()
        val_mse = float(np.mean((val_preds_np - y_val_np.flatten()) ** 2))
        val_mae = float(np.mean(np.abs(val_preds_np - y_val_np.flatten())))
        # Questa architettura è un baseline lineare di fallback, non la LSTM
        # addestrata; il nome viene riportato nei metadati per distinguerle.
        architecture_name = "DenseBaseline(36 -> 1) [Clinical Coefficients] -> ReLU"

    # Calcola l'hash crittografico del file salvato per garantirne l'integrità
    file_sha256 = compute_sha256(output_model_path)
    print(f"[✓] File ONNX salvato in: {output_model_path} (SHA256: {file_sha256[:16]}...)")
    
    # -------------------------------------------------------------------------
    # SALVATAGGIO DEI METADATI IN FORMATO JSON
    # -------------------------------------------------------------------------
    # Il JSON documenta identità, provenienza, formato d'ingresso, soglie
    # applicative e metriche del modello per audit e ispezione.
    # L'API legge da questo file almeno model_version per esporla nelle risposte;
    # i campi descrittivi non sostituiscono la validazione eseguita dal codice.
    metadata = {
        "model_name": "T1D-Insulin-Forecaster",
        "model_version": "1.0.0",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), # Data/ora UTC standard ISO 8601
        "sha256": file_sha256,
        "architecture": architecture_name,
        "dataset_source": "synthetic_diabetes_scaffolding",
        "training_note": "Modello prototipale di collaudo architetturale. Il retraining finale verra effettuato al rilascio del dataset clinico ufficiale.",
        "input_contract": {
            # Contratto atteso dal modello: numero di intervalli, canali
            # nell'ordine usato dal generatore e forma di un singolo campione.
            "timesteps": 12,
            "features_per_timestep": 3,
            "feature_names": ["glucose_mg_dl", "carbs_g", "past_insulin_u"],
            "expected_shape": [1, 12, 3]
        },
        "safety_guardrails": {
            # Valori documentali coerenti con i limiti impostati dall'API;
            # non configurano da soli il runtime.
            "min_dose_iu": 0.0,
            "max_dose_iu": 20.0,
            "warning_threshold_iu": 15.0
        },
        "validation_metrics": {
            # MSE penalizza maggiormente gli errori grandi; MAE è l'errore
            # assoluto medio. I risultati sono arrotondati per la leggibilità.
            "mse": round(val_mse, 4), # Errore quadratico arrotondato a 4 decimali
            "mae": round(val_mae, 4)  # Errore assoluto medio arrotondato a 4 decimali
        }
    }
    
    # Scrive i metadati su file con indentazione a 2 spazi per renderlo facilmente leggibile da un umano
    with open(output_metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2)
    print(f"[✓] Metadati salvati in: {output_metadata_path}")
    return metadata

if __name__ == "__main__":
    # Permette di lanciare lo script da terminale passando parametri personalizzati:
    # Esempio: python3 train_mock_model.py --epochs 50 --output-model custom.onnx
    # argparse usa i default indicati qui se l'opzione corrispondente non viene
    # passata; i due percorsi controllano gli artefatti scritti e --epochs
    # controlla il numero di iterazioni del training PyTorch.
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Addestramento offline ed export ONNX per T1D"
    )
    parser.add_argument("--output-model", default="model/model.onnx", help="Percorso del file .onnx da creare")
    parser.add_argument("--output-metadata", default="model/model_metadata.json", help="Percorso del file metadata .json")
    parser.add_argument("--epochs", type=int, default=25, help="Numero di iterazioni sull'intero dataset (epoche)")
    args: argparse.Namespace = parser.parse_args()
    
    train_and_export(args.output_model, args.output_metadata, args.epochs)
