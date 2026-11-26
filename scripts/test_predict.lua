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
    -- math.random(10000, 59999): Genera un numero intero casuale compreso in un pool di 50.000 pazienti
    -- (PATIENT_10000 .. PATIENT_59999), simulando i 50.000 clienti previsti dalla traccia di esame.
    local patient_id = "PATIENT_" .. math.random(10000, 59999)
    
    -- Simula una glicemia casuale compresa tra 4.5 e 10.5 mmol/L.
    local current_glucose = 4.5 + (math.random(0, 60) * 0.1)
    
    -- Simula un livello di insulina (da 0 a 8 U) e tasso di assorbimento CHO Hovorka (da 0.0 a 6.0).
    local recent_insulin = math.random(0, 8) * 1.0
    local recent_carbs = math.random(0, 60) * 0.1

    -- Genera il payload conforme allo schema OhioT1DM (13 campioni di glucosio in mmol/L + 14 esogeni).
    local payload = string.format([[{
  "patient_id": "%s",
  "glucose_history": [4.2, 4.0, 3.8, 3.6, 3.5, 3.7, 3.9, 4.3, 5.0, 5.5, 6.1, 6.7, %.1f],
  "exogenous_inputs": [%.1f, %.1f, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
}]], patient_id, current_glucose, recent_insulin, recent_carbs)

    return wrk.format("POST", "/predict", nil, payload)
end

-- response(): Callback eseguito da wrk alla ricezione di ciascuna risposta HTTP.
-- Ispeziona lo status code e segnala immediatamente su stderr qualsiasi codice diverso da 200 OK
-- (es. 308 redirect, 429 rate limit o 503 unavailability), prevenendo falsi positivi nei report SLA.
response = function(status, headers, body)
    if status ~= 200 then
        io.stderr:write(string.format("[WRK ATTENZIONE] Ricevuto codice HTTP %d (atteso 200 OK)\n", status))
    end
end
