document.addEventListener("DOMContentLoaded", async () => {
  const existingUser = await fetchCurrentUser();
  if (existingUser && ROLE_HOME[existingUser.role]) {
    if (redirectIfPasswordChangeRequired(existingUser)) return;
    window.location.href = ROLE_HOME[existingUser.role];
    return;
  }

  const form = document.getElementById("loginForm");
  const errBox = document.getElementById("loginError");
  const userInput = document.getElementById("username");
  const passInput = document.getElementById("password");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errBox.classList.add("hidden");
    try {
      const data = await apiFetch("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username: userInput.value.trim(),
          password: passInput.value,
        }),
      });
      if (redirectIfPasswordChangeRequired(data.user)) return;
      window.location.href = ROLE_HOME[data.user.role] || "/index.html";
    } catch (err) {
      errBox.textContent = err.message;
      errBox.classList.remove("hidden");
    }
  });
});
