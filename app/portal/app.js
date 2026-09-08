"use strict";

const state = { token: "", organization: "", project: "" };
const byId = (id) => document.getElementById(id);
const statusNode = byId("status");
const resultNode = byId("result");

function setStatus(message, isError = false) {
  statusNode.textContent = message;
  statusNode.classList.toggle("error", isError);
}

function showResult(value) {
  resultNode.textContent = JSON.stringify(value, null, 2);
}

function formValue(form, name) {
  return new FormData(form).get(name)?.toString().trim() || "";
}

function formSecret(form, name) {
  return new FormData(form).get(name)?.toString() || "";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.auth !== false) {
    if (!state.token) throw new Error("Log in before using this operation.");
    headers.set("Authorization", `Bearer ${state.token}`);
  }
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body,
    credentials: "same-origin",
    redirect: "error",
  });
  const document = await response.json().catch(() => null);
  if (!response.ok) {
    const code = document?.error?.code || `HTTP_${response.status}`;
    const message = document?.error?.message || "Request failed";
    throw new Error(`${code}: ${message}`.slice(0, 240));
  }
  return document;
}

function run(operation, successMessage, displayResult = true) {
  return operation()
    .then((value) => {
      if (displayResult && value !== undefined) showResult(value);
      setStatus(successMessage);
      return value;
    })
    .catch((error) => setStatus(error.message || "Request failed", true));
}

function populate(select, rows, emptyLabel) {
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = emptyLabel;
  select.append(empty);
  for (const row of rows) {
    const option = document.createElement("option");
    option.value = row.public_id;
    option.textContent = row.name || row.public_id;
    select.append(option);
  }
}
async function refreshOrganizations(selected = "") {
  const rows = await api("/v1/organizations");
  populate(byId("organization"), rows, "Select an organization");
  byId("organization").value = selected;
  state.organization = byId("organization").value;
  await refreshProjects();
  return rows;
}

async function refreshProjects(selected = "") {
  if (!state.organization) {
    populate(byId("project"), [], "Select an organization first");
    state.project = "";
    return [];
  }
  const rows = await api(
    `/v1/organizations/${encodeURIComponent(state.organization)}/projects`,
  );
  populate(byId("project"), rows, "Select a project");
  byId("project").value = selected;
  state.project = byId("project").value;
  return rows;
}

byId("register-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    const password = formSecret(form, "password");
    if (password !== formSecret(form, "confirmation")) {
      throw new Error("Passwords do not match.");
    }
    return api("/users/", {
      method: "POST",
      auth: false,
      json: {
        email: formValue(form, "email"),
        name: formValue(form, "name"),
        password,
      },
    });
  }, "Registration completed. Log in to continue.").finally(() => {
    form.elements.password.value = "";
    form.elements.confirmation.value = "";
  });
});

byId("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    const body = new URLSearchParams({
      username: formValue(form, "email"),
      password: formSecret(form, "password"),
    });
    const document = await api("/auth/login", {
      method: "POST",
      auth: false,
      body,
    });
    if (!document?.access_token) throw new Error("Login response is invalid.");
    state.token = document.access_token;
    byId("operations").hidden = false;
    byId("logout").disabled = false;
    await refreshOrganizations();
    showResult({ status: "authenticated" });
    return document;
  }, "Logged in. Select or create an organization.", false).finally(() => {
    form.elements.password.value = "";
  });
});

byId("logout").addEventListener("click", () => {
  state.token = "";
  state.organization = "";
  state.project = "";
  byId("event-form").elements.api_key.value = "";
  byId("operations").hidden = true;
  byId("logout").disabled = true;
  showResult({ status: "logged_out" });
  setStatus("Logged out. The in-memory token was cleared.");
});
byId("organizations-refresh").addEventListener("click", () => {
  run(() => refreshOrganizations(), "Organizations refreshed.");
});

byId("organization").addEventListener("change", (event) => {
  state.organization = event.target.value;
  run(() => refreshProjects(), "Projects refreshed.");
});

byId("organization-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    const created = await api("/v1/organizations", {
      method: "POST",
      json: { name: formValue(form, "name") },
    });
    form.reset();
    await refreshOrganizations(created.public_id);
    return created;
  }, "Organization created.");
});

byId("projects-refresh").addEventListener("click", () => {
  run(() => refreshProjects(), "Projects refreshed.");
});

byId("project").addEventListener("change", (event) => {
  state.project = event.target.value;
});

byId("project-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    if (!state.organization) throw new Error("Select an organization first.");
    const created = await api(
      `/v1/organizations/${encodeURIComponent(state.organization)}/projects`,
      { method: "POST", json: { name: formValue(form, "name") } },
    );
    form.reset();
    await refreshProjects(created.public_id);
    return created;
  }, "Project created.");
});

byId("api-key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    if (!state.project) throw new Error("Select a project first.");
    const expires = formValue(form, "expires");
    return api(`/v1/projects/${encodeURIComponent(state.project)}/api-keys`, {
      method: "POST",
      json: {
        name: formValue(form, "name"),
        scopes: ["events:write"],
        expires_in_days: expires ? Number(expires) : null,
      },
    });
  }, "Producer key created. Store the one-time key, then clear the result.");
});

byId("endpoint-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    if (!state.project) throw new Error("Select a project first.");
    const description = formValue(form, "description");
    // Comma-separated filters; an empty field means "receive every event"
    // and the key is omitted entirely (null also means unfiltered).
    const eventTypes = formValue(form, "event_types")
      .split(",")
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
    const body = {
      url: formValue(form, "url"),
      description: description || null,
      signature_scheme: formValue(form, "signature_scheme") || "legacy",
    };
    if (eventTypes.length > 0) body.event_types = eventTypes;
    return api(`/v1/projects/${encodeURIComponent(state.project)}/endpoints`, {
      method: "POST",
      json: body,
    });
  }, "Endpoint created. Store the signing secret, then clear the result.");
});
byId("event-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    const apiKey = formSecret(form, "api_key");
    try {
      if (!apiKey) throw new Error("Enter the producer API key.");
      let payload;
      try {
        payload = JSON.parse(formValue(form, "payload"));
      } catch {
        throw new Error("Payload must be valid JSON.");
      }
      return await api("/v1/events", {
        method: "POST",
        auth: false,
        headers: {
          "X-API-Key": apiKey,
          "Idempotency-Key": formValue(form, "idempotency_key"),
        },
        json: { type: formValue(form, "type"), payload },
      });
    } finally {
      form.elements.api_key.value = "";
    }
  }, "Event accepted.");
});

byId("deliveries-refresh").addEventListener("click", () => {
  run(async () => {
    if (!state.project) throw new Error("Select a project first.");
    return api(`/v1/projects/${encodeURIComponent(state.project)}/deliveries`);
  }, "Deliveries refreshed.");
});

byId("delivery-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const operation = event.submitter?.value || "inspect";
  run(async () => {
    if (!state.project) throw new Error("Select a project first.");
    const project = encodeURIComponent(state.project);
    const delivery = encodeURIComponent(formValue(form, "delivery_id"));
    if (operation === "replay") {
      return api(`/v1/projects/${project}/deliveries/${delivery}/replay`, {
        method: "POST",
      });
    }
    return api(`/v1/projects/${project}/deliveries/${delivery}`);
  }, operation === "replay" ? "Delivery replayed." : "Delivery inspected.");
});

byId("result-clear").addEventListener("click", () => {
  resultNode.textContent = "Cleared.";
});