let weeklyChart = null;
let riskChart = null;

document.addEventListener("DOMContentLoaded", async () => {
  if (!(await requireAuth("manager"))) return;
  document.getElementById("refreshManagerBtn").addEventListener("click", loadManagerDashboard);
  await loadManagerDashboard();
});

async function loadManagerDashboard() {
  const d = await apiFetch("/api/manager/dashboard");

  document.getElementById("kpiTotalIv").textContent = d.total_interventions;
  document.getElementById("kpiIvSplit").textContent =
    `Errore modello: ${d.interventions_by_model_error} | Comportamento: ${d.interventions_by_patient_behavior}`;
  document.getElementById("kpiMae").textContent =
    d.avg_prediction_mae_mmol !== null && d.avg_prediction_mae_mmol !== undefined
      ? `${d.avg_prediction_mae_mmol.toFixed(2)} mmol/L`
      : "n/d (dati insufficienti)";
  document.getElementById("kpiCohort").textContent =
    `Pazienti: ${d.total_patients} | Medici: ${d.total_doctors} | Regolarità: ${d.avg_meals_per_patient_per_day}/gg`;
  document.getElementById("kpiLatency").textContent = d.service_available ? `${d.avg_ml_latency_ms.toFixed(2)} ms` : "N/D";
  document.getElementById("kpiSla").textContent = d.service_available ? `${d.overall_sla_compliance_rate.toFixed(1)}%` : "0.0% (Offline)";
  document.getElementById("kpiPods").textContent =
    `Pod ML online: ${d.ml_pods_online} | Error rate: ${d.overall_error_rate.toFixed(2)}%`;

  const wCtx = document.getElementById("weeklyChart").getContext("2d");
  if (weeklyChart) weeklyChart.destroy();
  weeklyChart = new Chart(wCtx, {
    type: "bar",
    data: {
      labels: d.weekly_interventions.map((w) => w.week_label),
      datasets: [
        {
          label: "Comportamento Paziente",
          data: d.weekly_interventions.map((w) => w.patient_behavior_count),
          backgroundColor: "#ef8a17",
        },
        {
          label: "Errore Modello",
          data: d.weekly_interventions.map((w) => w.model_error_count),
          backgroundColor: "#145da0",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
    },
  });

  const rCtx = document.getElementById("riskChart").getContext("2d");
  if (riskChart) riskChart.destroy();
  const riskLabels = [
    "1: Ipo Severa",
    "2: Ipo Lieve",
    "3: Pre-target",
    "4: Ottimale",
    "5: Sopra Target",
    "6: Iper Elevata",
    "7: Iper Grave",
  ];
  riskChart = new Chart(rCtx, {
    type: "bar",
    data: {
      labels: riskLabels,
      datasets: [
        {
          label: "Occorrenze Aggregate",
          data: [1, 2, 3, 4, 5, 6, 7].map((k) => d.aggregated_risk_distribution[String(k)] || 0),
          backgroundColor: ["#b91c1c", "#ef4444", "#f59e0b", "#16a34a", "#f59e0b", "#ea580c", "#991b1b"],
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } },
    },
  });

  const tbody = document.getElementById("podsTableBody");
  if (d.ml_pods.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="muted">Nessun pod ML raggiungibile al momento.</td></tr>';
    return;
  }
  tbody.innerHTML = d.ml_pods
    .map(
      (p) => `
      <tr>
        <td><code>${escapeHtml(p.instance_id)}</code></td>
        <td><span class="badge ${p.service_status === "healthy" ? "badge-green" : "badge-red"}">${escapeHtml(p.service_status)}</span></td>
        <td>${p.uptime_seconds.toFixed(0)}</td>
        <td>${p.total_predictions}</td>
        <td>${p.avg_process_latency_ms.toFixed(2)} ms</td>
        <td>${p.max_process_latency_ms.toFixed(2)} ms</td>
        <td>${p.sla_compliance_rate.toFixed(1)}%</td>
        <td>${p.error_rate.toFixed(2)}%</td>
      </tr>`
    )
    .join("");
}
