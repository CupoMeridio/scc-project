let patientChart = null;
let activeRange = "week";

document.addEventListener("DOMContentLoaded", async () => {
  if (!(await requireAuth("patient"))) return;

  await loadSummary();
  await loadHistory(activeRange);

  document.querySelectorAll("#rangeButtons button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll("#rangeButtons button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      activeRange = btn.dataset.range;
      await loadHistory(activeRange);
    });
  });

  document.getElementById("mealForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const box = document.getElementById("mealOutcomeBox");
    box.className = "banner banner-info";
    box.textContent = "Calcolo previsione a +30 minuti in corso...";
    box.classList.remove("hidden");

    try {
      const carbs_g = parseFloat(document.getElementById("carbsInput").value);
      const insulin_u = parseFloat(document.getElementById("insulinInput").value || "0");
      const res = await apiFetch("/api/patient/meals", {
        method: "POST",
        body: JSON.stringify({ carbs_g, insulin_u }),
      });

      const style = outcomeStyle(res.outcome_code);
      box.className = `banner ${style.banner}`;
      // Valori nulli (esito NO_RECENT_DATA): nessun numero inventato, si mostra "n/d" (N5).
      const currentTxt =
        res.current_glucose_mmol_l !== null ? `${res.current_glucose_mmol_l.toFixed(2)} mmol/L` : "n/d";
      const forecastTxt =
        res.forecasted_glucose_mmol_l !== null
          ? `${res.forecasted_glucose_mmol_l.toFixed(2)} mmol/L (${res.forecasted_glucose_mg_dl.toFixed(0)} mg/dL)`
          : "n/d";
      box.innerHTML =
        `<strong>${escapeHtml(style.label)}</strong> — Attuale: ${currentTxt} | ` +
        `Previsione +30m: <strong>${forecastTxt}</strong><br>` +
        `${escapeHtml(res.outcome_message)}` +
        (res.doctor_flag ? "<br><em>Nota: evento segnalato silenziosamente al tuo diabetologo.</em>" : "");

      await loadSummary();
      await loadHistory(activeRange);
    } catch (err) {
      box.className = "banner banner-red";
      box.textContent = err.message;
    }
  });
});

async function loadSummary() {
  const s = await apiFetch("/api/patient/summary");
  document.getElementById("kpiGlucose").textContent =
    s.latest_glucose_mmol !== null ? `${s.latest_glucose_mmol.toFixed(2)} mmol/L` : "N/D";
  document.getElementById("kpiGlucoseSub").textContent =
    s.latest_glucose_mg_dl !== null ? `${s.latest_glucose_mg_dl.toFixed(0)} mg/dL (${formatDateTime(s.latest_reading_at)})` : "Nessuna lettura recente";
  document.getElementById("kpiDiet").textContent = `${s.diet_carb_limit_g.toFixed(0)} g`;
  document.getElementById("kpiDoctor").textContent = `Medico: ${s.doctor_name}`;
  document.getElementById("kpiMeals").textContent = s.total_meals_recorded;
  document.getElementById("kpiFlags").textContent = s.flagged_events_count;
}

async function loadHistory(range) {
  const data = await apiFetch(`/api/patient/history?range=${encodeURIComponent(range)}`);

  // Campiona fino a 80 punti per leggibilità del grafico
  const raw = data.readings;
  const step = Math.max(1, Math.floor(raw.length / 80));
  const sampled = raw.filter((_, idx) => idx % step === 0);

  const labels = sampled.map((r) => formatDateTime(r.recorded_at));
  const measured = sampled.map((r) => r.glucose_mmol);
  // Le previsioni a +30 min sono i valori reali prodotti dal modello alla dichiarazione
  // del pasto (RF10), non un campo inventato per singola lettura.
  const predicted = alignMealForecasts(sampled, data.meals);

  const ctx = document.getElementById("patientChart").getContext("2d");
  if (patientChart) patientChart.destroy();
  patientChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Glicemia Misurata (mmol/L)",
          data: measured,
          borderColor: "#162231",
          backgroundColor: "rgba(22, 34, 49, 0.08)",
          tension: 0.25,
          pointRadius: 2,
        },
        {
          label: "Previsione +30 min (mmol/L)",
          data: predicted,
          borderColor: "#145da0",
          borderDash: [4, 4],
          tension: 0.25,
          pointRadius: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { suggestedMin: 2, suggestedMax: 16 },
      },
    },
  });

  const tbody = document.getElementById("mealsTableBody");
  if (data.meals.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">Nessun pasto registrato in questo intervallo.</td></tr>';
    return;
  }
  tbody.innerHTML = data.meals
    .map((m) => {
      const st = outcomeStyle(m.outcome_code);
      return `
        <tr>
          <td>${escapeHtml(formatDateTime(m.declared_at))}</td>
          <td>${m.carbs_g.toFixed(0)} g</td>
          <td>${m.current_glucose_mmol !== null ? `${m.current_glucose_mmol.toFixed(2)} mmol/L` : "n/d"}</td>
          <td>${m.forecasted_glucose_mmol !== null ? `${m.forecasted_glucose_mmol.toFixed(2)} mmol/L` : "n/d"}</td>
          <td><span class="badge ${st.badge}">${escapeHtml(st.label)}</span></td>
          <td>${m.doctor_flag ? '<span class="badge badge-red">Sì</span>' : "No"}</td>
        </tr>`;
    })
    .join("");
}
