"""Self-contained Login_Page served by the Auth_Service Lambda (HTTPS).

The Auth_Service runs behind a Lambda **Function URL**, which is HTTPS by
default. Serving the login page from here (on ``GET``) instead of the S3 static
website endpoint solves two problems at once:

* **HTTPS** — S3 website endpoints are HTTP-only, and iOS/WhatsApp refuse to open
  ``http://`` links. The Function URL is ``https://``.
* **Same-origin** — the page and the ``POST /authenticate`` call live on the same
  URL, so there are no cross-origin/CORS issues.

The whole page (markup + CSS + JS) is inlined into a single HTML document so a
single ``GET`` returns everything (no separate ``styles.css`` / ``app.js``
requests). The browser script reads ``phone`` and ``token`` from the query string
and ``POST``s the credentials back to the **same** URL as JSON.
"""

from __future__ import annotations

# Single self-contained HTML document (inline <style> + <script>). The script
# POSTs to ``window.location.origin + window.location.pathname`` (same origin),
# so no endpoint placeholder substitution is needed.
LOGIN_PAGE_HTML: str = """<!DOCTYPE html>
<html lang="es-CO">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#002B5C" />
    <meta name="robots" content="noindex, nofollow" />
    <title>BTG ConnectAI &middot; Iniciar sesi&oacute;n</title>
    <style>
      :root {
        --btg-navy: #002b5c; --btg-navy-deep: #0a3d62; --btg-accent: #0a6ebd;
        --btg-ink: #1a2330; --btg-muted: #5b6878; --btg-card: #ffffff;
        --btg-border: #d9e0e8; --btg-error-bg: #fdecea; --btg-error-fg: #a4271c;
        --btg-success-bg: #e6f4ea; --btg-success-fg: #1e6b35;
        --btg-radius: 14px; --btg-shadow: 0 18px 40px rgba(0, 23, 56, 0.28);
      }
      * { box-sizing: border-box; }
      html, body { margin: 0; padding: 0; }
      body {
        min-height: 100vh;
        font-family: "Segoe UI", system-ui, -apple-system, Roboto, Helvetica, Arial, sans-serif;
        color: var(--btg-ink);
        background: linear-gradient(160deg, var(--btg-navy) 0%, var(--btg-navy-deep) 100%);
        -webkit-font-smoothing: antialiased;
      }
      .auth-shell { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px 16px; }
      .auth-card { width: 100%; max-width: 420px; background: var(--btg-card); border-radius: var(--btg-radius); box-shadow: var(--btg-shadow); padding: 28px 24px 20px; }
      .auth-brand { text-align: center; margin-bottom: 20px; }
      .auth-brand__logo { margin: 0; font-size: 1.6rem; font-weight: 800; letter-spacing: 0.5px; color: var(--btg-navy); }
      .auth-brand__logo span { font-weight: 400; color: var(--btg-accent); margin-left: 4px; }
      .auth-brand__product { margin: 2px 0 0; font-size: 0.78rem; letter-spacing: 3px; text-transform: uppercase; color: var(--btg-muted); }
      .auth-title { margin: 0 0 6px; font-size: 1.35rem; text-align: center; }
      .auth-subtitle { margin: 0 0 20px; font-size: 0.92rem; line-height: 1.4; color: var(--btg-muted); text-align: center; }
      .auth-status { border-radius: 10px; padding: 12px 14px; margin-bottom: 18px; font-size: 0.9rem; line-height: 1.4; }
      .auth-status--error { background: var(--btg-error-bg); color: var(--btg-error-fg); }
      .auth-status--success { background: var(--btg-success-bg); color: var(--btg-success-fg); }
      .auth-status--info { background: #eef3f9; color: var(--btg-navy-deep); }
      .auth-form { display: flex; flex-direction: column; gap: 16px; }
      .auth-field { display: flex; flex-direction: column; gap: 6px; }
      .auth-field label { font-size: 0.85rem; font-weight: 600; color: var(--btg-ink); }
      .auth-field input { width: 100%; padding: 12px 14px; font-size: 1rem; color: var(--btg-ink); background: #fff; border: 1px solid var(--btg-border); border-radius: 10px; transition: border-color 0.15s ease, box-shadow 0.15s ease; }
      .auth-field input:focus { outline: none; border-color: var(--btg-accent); box-shadow: 0 0 0 3px rgba(10, 110, 189, 0.18); }
      .auth-submit { margin-top: 4px; padding: 13px 16px; font-size: 1rem; font-weight: 700; color: #fff; background: var(--btg-navy); border: none; border-radius: 10px; cursor: pointer; transition: background 0.15s ease, transform 0.05s ease; }
      .auth-submit:hover:not(:disabled) { background: var(--btg-navy-deep); }
      .auth-submit:active:not(:disabled) { transform: translateY(1px); }
      .auth-submit:disabled { background: #9aa7b6; cursor: not-allowed; }
      .auth-footer { margin-top: 22px; text-align: center; }
      .auth-footer p { margin: 0; font-size: 0.72rem; color: var(--btg-muted); }
      @media (min-width: 600px) { .auth-card { padding: 36px 34px 24px; } .auth-title { font-size: 1.5rem; } }
    </style>
  </head>
  <body>
    <main class="auth-shell">
      <section class="auth-card" aria-labelledby="auth-title">
        <header class="auth-brand">
          <p class="auth-brand__logo">BTG<span>Pactual</span></p>
          <p class="auth-brand__product">ConnectAI</p>
        </header>
        <h1 id="auth-title" class="auth-title">Iniciar sesi&oacute;n</h1>
        <p class="auth-subtitle">Auten&iacute;cate para continuar con tu operaci&oacute;n bancaria en WhatsApp.</p>
        <div id="status" class="auth-status" role="status" aria-live="polite" hidden></div>
        <form id="login-form" class="auth-form" novalidate>
          <div class="auth-field">
            <label for="username">Usuario</label>
            <input type="text" id="username" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required />
          </div>
          <div class="auth-field">
            <label for="password">Contrase&ntilde;a</label>
            <input type="password" id="password" name="password" autocomplete="current-password" required />
          </div>
          <button type="submit" id="submit-btn" class="auth-submit">Ingresar</button>
        </form>
        <footer class="auth-footer">
          <p>Demo BTG Pactual Colombia &middot; Uso exclusivo de prueba</p>
        </footer>
      </section>
    </main>
    <script>
      "use strict";
      // Same-origin: POST to the same URL the page was served from.
      var AUTH_SERVICE_URL = window.location.origin + window.location.pathname;
      var MESSAGES = {
        invalidLink: "El enlace de inicio de sesi\\u00f3n expir\\u00f3 o no es v\\u00e1lido. Vuelve a WhatsApp y solicita uno nuevo.",
        missingFields: "Ingresa tu usuario y contrase\\u00f1a.",
        submitting: "Verificando credenciales...",
        success: "Autenticaci\\u00f3n exitosa, vuelve a WhatsApp para continuar.",
        invalidCredentials: "Usuario o contrase\\u00f1a incorrectos.",
        invalidToken: "El enlace de inicio de sesi\\u00f3n expir\\u00f3 o no es v\\u00e1lido.",
        network: "No pudimos conectar con el servicio. Revisa tu conexi\\u00f3n e int\\u00e9ntalo de nuevo.",
        generic: "Ocurri\\u00f3 un error inesperado. Int\\u00e9ntalo de nuevo."
      };
      function getLinkParams() {
        var p = new URLSearchParams(window.location.search);
        return { phone: p.get("phone") || "", token: p.get("token") || "" };
      }
      function showStatus(el, message, variant) {
        el.textContent = message;
        el.className = "auth-status auth-status--" + variant;
        el.hidden = false;
      }
      function messageForResponse(httpStatus) {
        if (httpStatus === 200) return { text: MESSAGES.success, variant: "success" };
        if (httpStatus === 401) return { text: MESSAGES.invalidCredentials, variant: "error" };
        if (httpStatus === 403) return { text: MESSAGES.invalidToken, variant: "error" };
        if (httpStatus === 400) return { text: MESSAGES.missingFields, variant: "error" };
        return { text: MESSAGES.generic, variant: "error" };
      }
      document.addEventListener("DOMContentLoaded", function () {
        var form = document.getElementById("login-form");
        var statusEl = document.getElementById("status");
        var submitBtn = document.getElementById("submit-btn");
        var usernameEl = document.getElementById("username");
        var passwordEl = document.getElementById("password");
        var params = getLinkParams();
        var phone = params.phone, token = params.token;
        if (!phone || !token) {
          showStatus(statusEl, MESSAGES.invalidLink, "error");
          submitBtn.disabled = true; usernameEl.disabled = true; passwordEl.disabled = true;
          return;
        }
        form.addEventListener("submit", async function (event) {
          event.preventDefault();
          var username = usernameEl.value.trim();
          var password = passwordEl.value;
          if (!username || !password) { showStatus(statusEl, MESSAGES.missingFields, "error"); return; }
          submitBtn.disabled = true;
          showStatus(statusEl, MESSAGES.submitting, "info");
          var response;
          try {
            response = await fetch(AUTH_SERVICE_URL, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ username: username, password: password, phone: phone, token: token })
            });
          } catch (err) {
            showStatus(statusEl, MESSAGES.network, "error");
            submitBtn.disabled = false; return;
          }
          var result = messageForResponse(response.status);
          showStatus(statusEl, result.text, result.variant);
          if (response.status === 200) { passwordEl.value = ""; return; }
          submitBtn.disabled = false;
        });
      });
    </script>
  </body>
</html>"""

__all__ = ["LOGIN_PAGE_HTML"]
