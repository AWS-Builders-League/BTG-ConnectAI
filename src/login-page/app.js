/*
 * BTG ConnectAI — Login_Page browser script (vanilla JS, no build step).
 *
 * Runs in the Bank_Client's browser after they open the WhatsApp login link:
 *
 *     {LOGIN_PAGE_URL}?phone=<urlencoded phone>&token=<callback-token>
 *
 * Responsibilities:
 *   1. Parse `phone` and `token` from the URL query string and keep them for
 *      the POST body. If either is missing, the link is invalid/expired and the
 *      form is disabled.
 *   2. On submit, POST JSON {username, password, phone, token} to the
 *      Auth_Service Lambda Function URL with Content-Type: application/json.
 *   3. Map the Auth_Service response to a Spanish status message.
 *
 * Auth_Service contract (see src/lambdas/auth_service/handler.py):
 *   200 {"status":"success"}                              -> autenticado
 *   401 {"status":"error","error":"invalid_credentials"}  -> usuario/clave malos
 *   403 {"status":"error","error":"invalid_token"}        -> enlace expirado
 *   400 {"status":"error","error":"bad_request"}          -> petición inválida
 *
 * AUTH_SERVICE_URL substitution:
 *   This is a static site served from S3, so the endpoint cannot be known at
 *   author time. We use the placeholder constant `__AUTH_SERVICE_URL__` below.
 *   The deploy pipeline (CI/CD task 15.8 / deploy) replaces the placeholder
 *   token with the real Auth_Service Lambda Function URL before/while uploading
 *   the file to the Login_Page S3 bucket (e.g. a `sed`/token-replace step that
 *   resolves the Function URL from the `infra` cross-stack contract). No code
 *   change is needed here — only the placeholder string is substituted.
 */

"use strict";

// Replaced at deploy time with the real Auth_Service Function URL.
const AUTH_SERVICE_URL = "__AUTH_SERVICE_URL__";

// Spanish UI copy, kept in one place.
const MESSAGES = {
  invalidLink:
    "El enlace de inicio de sesión expiró o no es válido. Vuelve a WhatsApp y solicita uno nuevo.",
  missingFields: "Ingresa tu usuario y contraseña.",
  submitting: "Verificando credenciales...",
  success: "Autenticación exitosa, vuelve a WhatsApp para continuar.",
  invalidCredentials: "Usuario o contraseña incorrectos.",
  invalidToken: "El enlace de inicio de sesión expiró o no es válido.",
  network:
    "No pudimos conectar con el servicio. Revisa tu conexión e inténtalo de nuevo.",
  generic: "Ocurrió un error inesperado. Inténtalo de nuevo.",
};

/** Read `phone` and `token` from the current URL query string. */
function getLinkParams() {
  const params = new URLSearchParams(window.location.search);
  return {
    phone: params.get("phone") || "",
    token: params.get("token") || "",
  };
}

/** Show a message in the status region with the given variant. */
function showStatus(el, message, variant) {
  el.textContent = message;
  el.className = "auth-status auth-status--" + variant;
  el.hidden = false;
}

/** Map an Auth_Service HTTP status + error code to a Spanish message. */
function messageForResponse(httpStatus, errorCode) {
  if (httpStatus === 200) return { text: MESSAGES.success, variant: "success" };
  if (httpStatus === 401)
    return { text: MESSAGES.invalidCredentials, variant: "error" };
  if (httpStatus === 403)
    return { text: MESSAGES.invalidToken, variant: "error" };
  if (httpStatus === 400)
    return { text: MESSAGES.missingFields, variant: "error" };
  return { text: MESSAGES.generic, variant: "error" };
}

document.addEventListener("DOMContentLoaded", function () {
  const form = document.getElementById("login-form");
  const statusEl = document.getElementById("status");
  const submitBtn = document.getElementById("submit-btn");
  const usernameEl = document.getElementById("username");
  const passwordEl = document.getElementById("password");

  const { phone, token } = getLinkParams();

  // Invalid/expired link: nothing to authenticate against. Disable the form.
  if (!phone || !token) {
    showStatus(statusEl, MESSAGES.invalidLink, "error");
    submitBtn.disabled = true;
    usernameEl.disabled = true;
    passwordEl.disabled = true;
    return;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();

    const username = usernameEl.value.trim();
    const password = passwordEl.value;

    if (!username || !password) {
      showStatus(statusEl, MESSAGES.missingFields, "error");
      return;
    }

    submitBtn.disabled = true;
    showStatus(statusEl, MESSAGES.submitting, "info");

    // Note: credentials are sent only in the POST body, never logged.
    let response;
    try {
      response = await fetch(AUTH_SERVICE_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, phone, token }),
      });
    } catch (err) {
      // Network / CORS / DNS failure — no HTTP response received.
      showStatus(statusEl, MESSAGES.network, "error");
      submitBtn.disabled = false;
      return;
    }

    const result = messageForResponse(response.status);
    showStatus(statusEl, result.text, result.variant);

    if (response.status === 200) {
      // Authenticated: clear the form and keep the button disabled so the
      // client returns to WhatsApp instead of re-submitting.
      passwordEl.value = "";
      return;
    }

    // Recoverable error — let the client try again.
    submitBtn.disabled = false;
  });
});
