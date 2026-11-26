document.addEventListener("DOMContentLoaded", async () => {
  const user = await fetchCurrentUser();
  if (!user) {
    window.location.href = "/index.html";
    return;
  }
  // Se l'utente non deve cambiare password, torna alla propria area.
  if (!user.must_change_password) {
    window.location.href = ROLE_HOME[user.role] || "/index.html";
    return;
  }

  const form = document.getElementById("changePasswordForm");
  const errBox = document.getElementById("changePasswordError");
  const oldInput = document.getElementById("oldPassword");
  const newInput = document.getElementById("newPassword");
  const confirmInput = document.getElementById("confirmPassword");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errBox.classList.add("hidden");

    if (newInput.value !== confirmInput.value) {
      errBox.textContent = "La conferma non coincide con la nuova password.";
      errBox.classList.remove("hidden");
      return;
    }

    try {
      await apiFetch("/api/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          old_password: oldInput.value,
          new_password: newInput.value,
        }),
      });
      window.location.href = ROLE_HOME[user.role] || "/index.html";
    } catch (err) {
      errBox.textContent = err.message;
      errBox.classList.remove("hidden");
    }
  });
});
