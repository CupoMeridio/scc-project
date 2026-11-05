-- FILE: scripts/test_predict.lua
-- RUOLO: Script Lua interpretato dal benchmark multithread 'wrk' per generare richieste HTTP POST.

-- wrk.method: Imposta il verbo HTTP globale da utilizzare per tutte le richieste generate (POST).
wrk.method = "POST"

-- wrk.headers: Tabella Lua contenente gli header HTTP da includere in ciascuna richiesta.
-- "Content-Type": "application/json" dice al server FastAPI che il payload trasmesso è in formato JSON.
wrk.headers["Content-Type"] = "application/json"

-- math.randomseed(os.time()): Inizializza il generatore di numeri pseudocasuali di Lua
-- con i secondi correnti dell'orologio di sistema (Epoch time), per evitare che ogni esecuzione
-- generi la stessa identica sequenza di valori.
math.randomseed(os.time())

-- request(): Funzione speciale predefinita di wrk.
-- Viene invocata da wrk prima di inviare ciascuna richiesta HTTP per personalizzarne il contenuto.
request = function()
    -- math.random(10000, 99999): Genera un numero intero casuale a 5 cifre.
    -- Simula 50.000 pazienti diversi che effettuano richieste concorrenti (Requisito SLA della traccia).
    local patient_id = "PATIENT_" .. math.random(10000, 99999)
    
    -- Simula una glicemia casuale compresa tra 120 e 200 mg/dL (120 + numero da 0 a 80).
    local current_glucose = 120.0 + (math.random(0, 80) * 1.0)
    
    -- Simula un'assunzione di carboidrati da 0 a 50 grammi.
    local recent_carbs = math.random(0, 50) * 1.0

    -- string.format(): Formatta una stringa sostituendo i segnaposto:
    -- - %s: Sostituito dalla stringa patient_id
    -- - %.1f: Sostituito da un numero in virgola mobile con 1 cifra decimale (es. 145.2)
    -- Genera la matrice clinica esatta di 12 intervalli temporali con 3 feature per intervallo.
    local payload = string.format([[{
  "patient_id": "%s",
  "features": [
    [110.0, 0.0, 0.0],
    [112.0, 0.0, 0.0],
    [115.0, 0.0, 0.0],
    [120.0, 0.0, 0.0],
    [130.0, 40.0, 0.0],
    [150.0, 0.0, 0.0],
    [165.0, 0.0, 0.0],
    [160.0, 0.0, 0.0],
    [150.0, 0.0, 0.0],
    [140.0, 0.0, 0.0],
    [130.0, 0.0, 0.0],
    [%.1f, %.1f, 0.0]
  ]
}]], patient_id, current_glucose, recent_carbs)

    -- wrk.format(method, path, headers, body): Funzione integrata in wrk che assembla la richiesta HTTP raw:
    -- - method: "POST"
    -- - path: "/predict" (l'endpoint sottoposto al carico di richieste)
    -- - headers: nil (usa gli header globali definiti sopra in wrk.headers)
    -- - body: payload (la stringa JSON appena formattata)
    return wrk.format("POST", "/predict", nil, payload)
end
