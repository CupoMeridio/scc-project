"""
FILE: app/config.py
RUOLO: Gestione centralizzata delle variabili di configurazione dell'applicazione.
"""

import os  # Modulo standard di Python per interagire con il sistema operativo


class Settings:
    """
    Classe singleton che raccoglie tutte le configurazioni dell'applicazione.

    METODOLOGIA THE TWELVE-FACTOR APP (Fattore III: Config):
    I parametri di configurazione NON devono mai essere cablati (hardcoded) nel codice.
    Utilizzo di os.getenv(NOME_VARIABILE, VALORE_DEFAULT):
    - Se la variabile d'ambiente esiste nel sistema (es. iniettata da Kubernetes tramite il file YAML),
      il processo usa quel valore dinamico.
    - Se non esiste (esecuzione in locale), viene usato il valore di fallback specificato.
    """

    # Nome dell'applicazione e versione visualizzati nell'interfaccia interattiva Swagger (OpenAPI)
    PROJECT_NAME: str = "OhioT1DM Glucose Forecasting & Risk Classification API"
    VERSION: str = "1.0.0"

    # Percorso del file del modello ONNX.
    # Default: "model/model.onnx" (percorso relativo che funziona sia in locale dalla radice
    # del progetto, sia nel container Docker dove la cartella di lavoro WORKDIR è impostata a /app).
    # Il file model.onnx.data (pesi esterni) deve risiedere nella stessa cartella.
    MODEL_PATH: str = os.getenv("MODEL_PATH", "model/model.onnx")

    # Percorso del file JSON contenente metadati (versione, hash SHA256, parametri di normalizzazione).
    METADATA_PATH: str = os.getenv("METADATA_PATH", "model/model_metadata.json")

    # NOTA: non esiste una modalità "mock". L'applicazione serve esclusivamente il modello ONNX reale:
    # se l'artefatto o i parametri di normalizzazione mancano, /ready e /predict rispondono 503.

    # Indirizzo IP di ascolto:
    # "0.0.0.0" (INADDR_ANY) ordina al server di ascoltare su TUTTE le schede di rete disponibili.
    # ATTENZIONE: Se impostassimo "127.0.0.1" (localhost), il server ascolterebbe solo dall'interno
    # del container stesso e NGINX o Kubernetes non riuscirebbero a comunicare con l'API dall'esterno!
    HOST: str = os.getenv("HOST", "0.0.0.0")

    # Porta di ascolto TCP (default: 8000).
    # Nei sistemi Linux, solo l'utente 'root' può aprire porte inferiori a 1024 (es. 80 o 443).
    # Usare la porta 8000 permette all'applicazione di girare come utente NON-ROOT (regola di sicurezza).
    PORT: int = int(os.getenv("PORT", "8000"))

    # Livello di severità dei log di sistema: "INFO", "WARNING", "ERROR", "DEBUG".
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()


# Istanza globale importabile da tutti i moduli del progetto (es. from app.config import settings)
settings = Settings()
