let doctorChart = null;
let selectedPatientId = null;
let activeDocRange = "week";

document.addEventListener("DOMContentLoaded", async () => {
  if (!(await requireAuth("doctor"))) return;

  document.getElementById("toggleRegisterBtn").addEventListener("click", () => {
    document.getElementById("registerCard").classList.toggle("hidden");
  });

  document.getElementById("ivType").addEventListener("change", (e) => {
    const wrap = document.getElementById("ivNewDietWrap");
    wrap.classList.toggle("hidden", e.target.value !== "diet_change");
  });

  document.querySelectorAll("#docRangeButtons button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("#docRangeButtons button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      activeDocRange = btn.dataset.range;
      if (selectedPatientId) await loadPatientDetail(selectedPatientId, activeDocRange);
    });
  });

  document.getElementById("registerPatientForm").addEventListener("submit", onRegisterPatient);
  document.getElementById("dietForm").addEventListener("submit", onUpdateDiet);
  document.getElementById("interventionForm").addEventListener("submit", onCreateIntervention);

  await loadPatientsList(true);
});

async function loadPatientsList(selectFirst = false) {
  const patients = await apiFetch("/api/doctor/patients");
  const listEl = document.getElementById("patientsList");

  if (patients.length === 0) {
    listEl.innerHTML = '<p class="muted">Nessun paziente assegnato.</p>';
    return;
  }

  if (selectFirst && !selectedPatientId) {
    selectedPatientId = patients[0].patient_id;
  }

  listEl.innerHTML = patients
    .map(
      (p) => `
      <div class="patient-item ${p.patient_id === selectedPatientId ? "selected" : ""}" data-id="${p.patient_id}">
        <div>
          <strong>${escapeHtml(p.full_name)}</strong> <span class="muted">(#${escapeHtml(p.patient_code)})</span><br>
          <span class="muted">${escapeHtml(p.status_summary)}</span>
        </div>
        <span class="dot dot-${escapeHtml(p.status_color)}" title="${escapeHtml(p.status_summary)}"></span>
      </div>`
    )
    .join("");

  listEl.querySelectorAll(".patient-item").forEach((el) => {
    el.addEventListener("click", async () => {
      selectedPatientId = Number(el.dataset.id);
      listEl.querySelectorAll(".patient-item").forEach((item) => item.classList.remove("selected"));
      el.classList.add("selected");
      await loadPatientDetail(selectedPatientId, activeDocRange);
    });
  });

  if (selectedPatientId) {
    await loadPatientDetail(selectedPatientId, activeDocRange);
  }
}

async function loadPatientDetail(patientId, range) {
  const d = await apiFetch(`/api/doctor/patients/${patientId}?range=${encodeURIComponent(range)}`);

  document.getElementById("detailName").textContent = `${d.full_name} (Codice ${d.patient_code})`;
  document.getElementById("detailMeta").textContent =
    `Email: ${d.email} | Dieta attuale: ${d.diet_carb_limit_g} g | Target: ${d.target_min_mmol}–${d.target_max_mmol} mmol/L | ` +
    `Pasti sopra dieta: ${d.trends.above_diet_meals_pct}% | Regolarità: ${d.trends.meal_regularity_per_day} pasti/giorno`;

  document.getElementById("dietCarbLimit").value = d.diet_carb_limit_g;
  document.getElementById("dietMinMmol").value = d.target_min_mmol;
  document.getElementById("dietMaxMmol").value = d.target_max_mmol;

  const meanErr = d.trends.mean_prediction_error_mmol;
  const hasMeanErr = meanErr !== null && meanErr !== undefined;
  const trendBox = document.getElementById("trendAlert");
  trendBox.classList.remove("hidden");
  trendBox.className = `banner ${(hasMeanErr && Math.abs(meanErr) > 0.35) || d.trends.flagged_events_count > 0 ? "banner-orange" : "banner-green"}`;
  trendBox.innerHTML =
    `<strong>Analisi Trend (RF12):</strong> ${escapeHtml(d.trends.model_bias_label)} ` +
    `(errore medio ${hasMeanErr ? `${meanErr >= 0 ? "+" : ""}${meanErr.toFixed(2)}` : "n/d"} mmol/L) — ` +
    `Segnalazioni ipo/iper nel periodo: <strong>${d.trends.flagged_events_count}</strong>.`;

  const raw = d.readings;
  const step = Math.max(1, Math.floor(raw.length / 80));
  const sampled = raw.filter((_, idx) => idx % step === 0);
  const labels = sampled.map((r) => formatDateTime(r.recorded_at));
  // Le previsioni a +30 min sono i valori reali prodotti dal modello alla dichiarazione
  // del pasto (RF10), non un campo inventato per singola lettura.
  const predicted = alignMealForecasts(sampled, d.meals);

  const ctx = document.getElementById("doctorChart").getContext("2d");
  if (doctorChart) doctorChart.destroy();
  doctorChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Misurato CGM (mmol/L)",
          data: sampled.map((r) => r.glucose_mmol),
          borderColor: "#162231",
          tension: 0.25,
          pointRadius: 2,
        },
        {
          label: "Predetto +30m (mmol/L)",
          data: predicted,
          borderColor: "#145da0",
          tension: 0.25,
          pointRadius: 2,
        },
        {
          label: "Soglia Target Max",
          data: sampled.map(() => d.target_max_mmol),
          borderColor: "#dc2626",
          borderDash: [6, 4],
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { y: { suggestedMin: 2, suggestedMax: 16 } },
    },
  });

  const ivBody = document.getElementById("interventionsTable");
  if (d.interventions.length === 0) {
    ivBody.innerHTML = '<tr><td colspan="4" class="muted">Nessun intervento registrato per questo paziente.</td></tr>';
  } else {
    ivBody.innerHTML = d.interventions
      .map(
        (iv) => `
        <tr>
          <td>${escapeHtml(formatDateTime(iv.created_at))}</td>
          <td>${escapeHtml(iv.intervention_type === "diet_change" ? "Cambio Dieta" : "Promemoria")}</td>
          <td><span class="badge ${iv.cause === "model_error" ? "badge-orange" : "badge-red"}">${escapeHtml(
            iv.cause === "model_error" ? "Errore Modello" : "Comportamento Paziente"
          )}</span></td>
          <td>${escapeHtml(iv.notes)}</td>
        </tr>`
      )
      .join("");
  }
}

async function onRegisterPatient(e) {
  e.preventDefault();
  const msgBox = document.getElementById("registerMsg");
  msgBox.classList.add("hidden");
  try {
    const created = await apiFetch("/api/doctor/patients", {
      method: "POST",
      body: JSON.stringify({
        full_name: document.getElementById("regFullName").value.trim(),
        username: document.getElementById("regUsername").value.trim(),
        email: document.getElementById("regEmail").value.trim(),
        patient_code: document.getElementById("regCode").value.trim(),
        diet_carb_limit_g: parseFloat(document.getElementById("regDiet").value),
      }),
    });
    // La password temporanea è generata dal server e mostrata una sola volta (Q5, N3).
    msgBox.className = "banner banner-green";
    msgBox.innerHTML =
      `Paziente ${escapeHtml(created.full_name)} registrato. ` +
      `Password temporanea (mostrata una sola volta, comunicala al paziente): ` +
      `<strong>${escapeHtml(created.temporary_password)}</strong>. ` +
      `Il paziente dovrà cambiarla al primo accesso.`;
    msgBox.classList.remove("hidden");
    selectedPatientId = created.patient_id;
    e.target.reset();
    await loadPatientsList(false);
  } catch (err) {
    msgBox.className = "banner banner-red";
    msgBox.textContent = err.message;
    msgBox.classList.remove("hidden");
  }
}

async function onUpdateDiet(e) {
  e.preventDefault();
  if (!selectedPatientId) return;
  const msg = document.getElementById("dietMsg");
  try {
    await apiFetch(`/api/doctor/patients/${selectedPatientId}/diet`, {
      method: "PATCH",
      body: JSON.stringify({
        diet_carb_limit_g: parseFloat(document.getElementById("dietCarbLimit").value),
        target_min_mmol: parseFloat(document.getElementById("dietMinMmol").value),
        target_max_mmol: parseFloat(document.getElementById("dietMaxMmol").value),
      }),
    });
    msg.className = "banner banner-green";
    msg.textContent = "Dieta aggiornata con successo.";
    msg.classList.remove("hidden");
    await loadPatientsList(false);
  } catch (err) {
    msg.className = "banner banner-red";
    msg.textContent = err.message;
    msg.classList.remove("hidden");
  }
}

async function onCreateIntervention(e) {
  e.preventDefault();
  if (!selectedPatientId) return;
  const msg = document.getElementById("ivMsg");
  const ivType = document.getElementById("ivType").value;
  const newDietRaw = document.getElementById("ivNewDiet").value;
  try {
    await apiFetch(`/api/doctor/patients/${selectedPatientId}/interventions`, {
      method: "POST",
      body: JSON.stringify({
        intervention_type: ivType,
        cause: document.getElementById("ivCause").value,
        notes: document.getElementById("ivNotes").value.trim(),
        new_diet_carb_limit_g: ivType === "diet_change" && newDietRaw ? parseFloat(newDietRaw) : null,
      }),
    });
    msg.className = "banner banner-green";
    msg.textContent = "Intervento clinico registrato.";
    msg.classList.remove("hidden");
    document.getElementById("ivNotes").value = "";
    await loadPatientsList(false);
  } catch (err) {
    msg.className = "banner banner-red";
    msg.textContent = err.message;
    msg.classList.remove("hidden");
  }
}
