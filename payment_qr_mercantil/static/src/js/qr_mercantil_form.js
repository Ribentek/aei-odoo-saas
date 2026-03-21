/** @odoo-module **/
/**
 * QR Mercantil — frontend polling + demo simulation
 * Checks tx state every 3s and redirects on success/failure.
 * In demo mode a "Simular Pago" button is wired to /payment/qr_mercantil/simulate.
 */

import { browser } from "@web/core/browser/browser";

const POLL_INTERVAL_MS = 3000;

function startQRPolling() {
    const refInput = document.getElementById("qr_mercantil_reference");
    const statusUrlInput = document.getElementById("qr_mercantil_status_url");
    const landingInput = document.getElementById("qr_mercantil_landing");
    const msgEl = document.getElementById("qr_mercantil_status_msg");

    if (!refInput || !statusUrlInput) return; // Not on QR Mercantil form

    const reference = refInput.value;
    const statusUrl = statusUrlInput.value;
    const landingRoute = (landingInput && landingInput.value) || "/payment/status";

    if (!reference || !statusUrl) return;

    let attempts = 0;
    const MAX_ATTEMPTS = 200; // 200 x 3s = 10 minutes timeout

    const intervalId = setInterval(async () => {
        attempts++;
        if (attempts > MAX_ATTEMPTS) {
            clearInterval(intervalId);
            if (msgEl) {
                msgEl.innerHTML =
                    '<span class="text-warning">Tiempo de espera agotado. Si ya pagaste, el pedido se confirmara automaticamente.</span>';
            }
            return;
        }

        try {
            const resp = await fetch(statusUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: { reference } }),
            });
            const data = await resp.json();
            const result = data.result || {};
            const state = result.state;

            // In demo mode the server never confirms automatically — stop polling
            // and instruct the user to click the "Simular Pago" button.
            if (result.is_demo) {
                clearInterval(intervalId);
                if (msgEl) {
                    msgEl.innerHTML =
                        '<span class="text-info">🔧 <strong>Modo Demo activo.</strong> Haz clic en <em>✅ Simular Pago (Demo)</em> para confirmar la transacción.</span>';
                }
                // Scroll the simulate button into view so the user sees it
                const simulateBtn = document.getElementById("qr_mercantil_simulate_btn");
                if (simulateBtn) simulateBtn.scrollIntoView({ behavior: "smooth", block: "center" });
                return;
            }

            if (state === "done") {
                clearInterval(intervalId);
                if (msgEl) {
                    msgEl.innerHTML =
                        '<span class="text-success">Pago confirmado! Redirigiendo...</span>';
                }
                browser.setTimeout(() => {
                    window.location.href = landingRoute;
                }, 1500);
            } else if (state === "cancel" || state === "error") {
                clearInterval(intervalId);
                if (msgEl) {
                    msgEl.innerHTML =
                        '<span class="text-danger">Pago cancelado o fallido.</span>';
                }
            }
        } catch (e) {
            // Network error - keep polling silently
            console.debug("QR Mercantil poll error:", e);
        }
    }, POLL_INTERVAL_MS);

    // -- Demo mode: wire up "Simular Pago" button ----------------------------
    const simulateBtn = document.getElementById("qr_mercantil_simulate_btn");
    const simulateUrlInput = document.getElementById("qr_mercantil_simulate_url");

    if (simulateBtn && simulateUrlInput) {
        const simulateUrl = simulateUrlInput.value;

        // Guard flag: prevents double-execution from rapid double-clicks or slow network.
        // Set synchronously before the first await so any second click is rejected
        // before any fetch() is issued.
        let isSimulating = false;

        simulateBtn.addEventListener("click", async () => {
            if (isSimulating) return;  // Reentrada bloqueada — ya hay un request en vuelo
            isSimulating = true;
            simulateBtn.disabled = true;
            simulateBtn.textContent = "Procesando...";

            try {
                const resp = await fetch(simulateUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        jsonrpc: "2.0",
                        method: "call",
                        params: { reference },
                    }),
                });
                const data = await resp.json();
                const result = data.result || {};

                if (result.status === "ok" || result.status === "already_done") {
                    clearInterval(intervalId);
                    if (msgEl) {
                        msgEl.innerHTML =
                            '<span class="text-success">Pago simulado correctamente. Redirigiendo...</span>';
                    }
                    browser.setTimeout(() => {
                        window.location.href = result.landing_route || landingRoute;
                    }, 1200);
                } else {
                    isSimulating = false;  // Liberar guard: el usuario puede reintentar
                    simulateBtn.disabled = false;
                    simulateBtn.textContent = "✅ Simular Pago (Demo)";
                    const errMsg = result.message || "error desconocido";
                    if (msgEl) {
                        msgEl.innerHTML =
                            '<span class="text-danger">Error al simular: ' + errMsg + '</span>';
                    }
                }
            } catch (e) {
                console.error("QR Mercantil simulate error:", e);
                isSimulating = false;  // Liberar guard: el usuario puede reintentar
                simulateBtn.disabled = false;
                simulateBtn.textContent = "✅ Simular Pago (Demo)";
                if (msgEl) {
                    msgEl.innerHTML =
                        '<span class="text-danger">Error de red al simular pago.</span>';
                }
            }
        });
    }
}

// Start when DOM is ready
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startQRPolling);
} else {
    startQRPolling();
}
