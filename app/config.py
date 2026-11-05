"""
FILE: app/config.py
RUOLO: Gestione centralizzata delle variabili di configurazione dell'applicazione.
"""

import os # Modulo standard di Python per interagire con il sistema operativo

class Settings:
    """
    Classe singleton che raccoglie tutte le configurazioni del microservizio.
    
    METODOLOGIA THE TWELVE-FACTOR APP (Fattore III: Config):
    I parametri di configurazione NON devono mai essere cablati (hardcoded) nel codice.
    Utilizzo di os.getenv(NOME_VARIABILE, VALORE_DEFAULT):
    - Se la variabile d'ambiente esiste nel sistema (es. iniettata da Kubernetes tramite il file YAML),
      il programma usa quel valore dinamico.
    - Se non esiste (es. esecuzione in locale), viene usato il valore di fallback sicuro specificato come secondo argomento.
    """

    # Nome dell'applicazione e versione visualizzati nell'interfaccia interattiva Swagger (OpenAPI)
    PROJECT_NAME: str = "T1D Insulin Forecasting API"
    VERSION: str = "1.0.0"

    # Percorso del file del modello ONNX.
    # Default: "model/model.onnx" (percorso relativo che funziona sia in locale dalla radice
    # del progetto, sia nel container Docker dove la cartella di lavoro WORKDIR e impostata a /app).
    MODEL_PATH: str = os.getenv("MODEL_PATH", "model/model.onnx")

    # Percorso del file JSON contenente i metadati (versione, hash SHA256, metriche)
    METADATA_PATH: str = os.getenv("METADATA_PATH", "model/model_metadata.json")

    # Modalita operativa predefinita: "mock" per sviluppo locale immediato out-of-the-box,
    # oppure "real" per produzione e collaudo cluster (iniettata via env nei manifest K8s).
    # .lower(): converte in minuscolo (es. "REAL" -> "real") per evitare errori di battitura.
    # COMPORTAMENTO CRUCIALE:
    # - In modalita "real", se il file model.onnx manca o e corrotto, la readiness probe (/ready)
    #   risponde con errore HTTP 503, impedendo a Kubernetes di mandare richieste a un pod vuoto.
    # - In modalita "mock", se il file manca, l'API usa una formula fisiologica di riserva e risponde 200.
    MODEL_MODE: str = os.getenv("MODEL_MODE", "mock").lower()

    # Safety Guardrails clinici (Dose massima insuperabile di insulina).
    # float(): converte la stringa letta dall'ambiente (es. "20.0") in un numero decimale in virgola mobile.
    # 20.0 Unità è un tetto massimo di buon senso per un singolo bolo umano; valori superiori sono quasi sempre
    # causati da sensori impazziti o allucinazioni del modello neurale.
    MAX_INSULIN_DOSE: float = float(os.getenv("MAX_INSULIN_DOSE", "20.0"))
    
    # Se la dose stimata supera 15.0 Unità, viene registrato un avviso speciale nei log di audit.
    WARNING_INSULIN_DOSE: float = float(os.getenv("WARNING_INSULIN_DOSE", "15.0"))

    # Indirizzo IP di ascolto:
    # "0.0.0.0" (INADDR_ANY) ordina al server di ascoltare su TUTTE le schede di rete disponibili.
    # ATTENZIONE: Se impostassimo "127.0.0.1" (localhost), il server ascolterebbe solo dall'interno
    # del container stesso e NGINX o Kubernetes non riuscirebbero a comunicare con l'API dall'esterno!
    HOST: str = os.getenv("HOST", "0.0.0.0")

    # Porta di ascolto TCP (default: 8000).
    # int(): converte la stringa "8000" in intero.
    # Nei sistemi Linux, solo l'utente 'root' può aprire porte inferiori a 1024 (es. 80 o 443).
    # Usare la porta 8000 permette all'applicazione di girare come utente NON-ROOT (regola di sicurezza).
    PORT: int = int(os.getenv("PORT", "8000"))

    # Livello di severità dei log di sistema: "INFO", "WARNING", "ERROR", "DEBUG".
    # .upper(): converte in maiuscolo (es. "info" -> "INFO") per compatibilità con il modulo logging di Python.
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Istanza globale importabile da tutti gli altri moduli del progetto (es. from app.config import settings)
settings = Settings()
