/* Helper condivisi: sessione su cookie HttpOnly (nessun token in localStorage) e rendering sicuro */
const ROLE_HOME = {
  patient: "/patient.html",
  doctor: "/doctor.html",
  manager: "/manager.html",
};

const CHANGE_PASSWORD_PAGE = "/change-password.html";

/** Reindirizza al cambio password se l'account ha una password iniziale temporanea. */
function redirectIfPasswordChangeRequired(user) {
  if (user && user.must_change_password && window.location.pathname !== CHANGE_PASSWORD_PAGE) {
    window.location.href = CHANGE_PASSWORD_PAGE;
    return true;
  }
  return false;
}

async function logout() {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
    });
  } catch {
    // Anche in caso di errore di rete reindirizza al login
  }
  window.location.href = "/index.html";
}

async function fetchCurrentUser() {
  const res = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (!res.ok) return null;
  return res.json();
}

async function requireAuth(expectedRole) {
  const user = await fetchCurrentUser();
  if (!user) {
    window.location.href = "/index.html";
    return null;
  }
  if (expectedRole && user.role !== expectedRole) {
    window.location.href = ROLE_HOME[user.role] || "/index.html";
    return null;
  }
  if (redirectIfPasswordChangeRequired(user)) return null;
  const userEl = document.getElementById("navUserName");
  if (userEl) userEl.textContent = user.full_name;
  const logoutBtn = document.getElementById("logoutBtn");
  if (logoutBtn) logoutBtn.addEventListener("click", logout);
  return user;
}

async function apiFetch(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  const res = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (res.status === 401) {
    window.location.href = "/index.html";
    throw new Error("Sessione non autenticata o scaduta");
  }
  const data = await res.json().catch(() => ({}));
  // Il server impone il cambio password (N3): reindirizza sulla pagina dedicata.
  if (res.status === 403 && data.detail && data.detail.code === "PASSWORD_CHANGE_REQUIRED") {
    window.location.href = CHANGE_PASSWORD_PAGE;
    throw new Error("È necessario cambiare la password prima di continuare.");
  }
  if (!res.ok) {
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : (detail && detail.message) || `Errore HTTP ${res.status}`;
    throw new Error(message);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDateTime(iso) {
  if (!iso) return "N/D";
  const d = new Date(iso);
  return d.toLocaleString("it-IT", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Allinea le previsioni a +30 min dei pasti (dato reale del modello, RF10) ai punti del
 * grafico CGM campionati: ogni pasto è posizionato sull'indice temporale più vicino.
 */
function alignMealForecasts(sampledReadings, meals) {
  const points = new Array(sampledReadings.length).fill(null);
  if (sampledReadings.length === 0) return points;
  const times = sampledReadings.map((r) => new Date(r.recorded_at).getTime());
  for (const meal of meals || []) {
    if (meal.forecasted_glucose_mmol === null || meal.forecasted_glucose_mmol === undefined) continue;
    const declared = new Date(meal.declared_at).getTime();
    let bestIdx = -1;
    let bestDist = Infinity;
    for (let i = 0; i < times.length; i++) {
      const dist = Math.abs(times[i] - declared);
      if (dist < bestDist) {
        bestDist = dist;
        bestIdx = i;
      }
    }
    if (bestIdx >= 0) points[bestIdx] = meal.forecasted_glucose_mmol;
  }
  return points;
}

function outcomeStyle(code) {
  if (code === "PROCEED_REGULAR") {
    return { badge: "badge-green", banner: "banner-green", label: "Regolare" };
  }
  if (code === "PROCEED_WITH_WARNING") {
    return { badge: "badge-orange", banner: "banner-orange", label: "Avviso Dieta / Margine" };
  }
  if (code === "RISK_OF_HYPOGLYCEMIA") {
    return { badge: "badge-red", banner: "banner-red", label: "Rischio Ipoglicemia" };
  }
  if (code === "RISK_OF_HYPERGLYCEMIA") {
    return { badge: "badge-red", banner: "banner-red", label: "Rischio Iperglicemia" };
  }
  if (code === "NO_RECENT_DATA") {
    return { badge: "badge-orange", banner: "banner-orange", label: "Dati Sensore Non Recenti" };
  }
  return { badge: "badge-red", banner: "banner-red", label: "Esito Sconosciuto" };
}
