/**
 * LinkLite Minimal Frontend & Analytics Dashboard
 *
 * Security Note (XSS Prevention):
 * All dynamic content from user input and API responses is inserted into the
 * DOM strictly using `.textContent` or element property assignments (e.g. .href).
 * innerHTML is NEVER used for untrusted data, preventing Cross-Site Scripting (XSS).
 */

let clicksChartInstance = null;

// Helper: Safely format ISO dates
function formatDate(isoString) {
    if (!isoString) return "-";
    try {
        const d = new Date(isoString);
        return d.toLocaleString();
    } catch {
        return isoString;
    }
}

// Helper: Format error message from fetch response
async function parseErrorMessage(response) {
    const status = response.status;
    let detail = "";
    try {
        const data = await response.json();
        if (data && data.detail) {
            if (Array.isArray(data.detail)) {
                // Pydantic validation error list
                detail = data.detail.map(e => e.msg || JSON.stringify(e)).join(", ");
            } else {
                detail = String(data.detail);
            }
        }
    } catch {
        detail = response.statusText || "Unknown error";
    }

    if (status === 409) {
        return `Conflict (409): ${detail || "This custom alias is already in use. Please choose another."}`;
    }
    if (status === 422) {
        return `Validation Error (422): ${detail || "Invalid URL or input parameters."}`;
    }
    if (status === 429) {
        const retryAfter = response.headers.get("Retry-After");
        const retryMsg = retryAfter ? ` Please retry after ${retryAfter} seconds.` : "";
        return `Rate Limit Exceeded (429): You have sent too many requests.${retryMsg}`;
    }
    if (status === 404) {
        return `Not Found (404): ${detail || "No link found for the specified code or alias."}`;
    }
    if (status === 410) {
        return `Expired (410): ${detail || "This link has reached its expiration time and is no longer active."}`;
    }

    return `Error (${status}): ${detail || "An unexpected error occurred."}`;
}

// -------------------------------------------------------------
// 1. Create Link Form Handler
// -------------------------------------------------------------
const createForm = document.getElementById("create-form");
const createSubmitBtn = document.getElementById("create-submit-btn");
const createErrorAlert = document.getElementById("create-error-alert");
const createResultBox = document.getElementById("create-result-box");
const shortUrlLink = document.getElementById("short-url-link");
const resultLongUrl = document.getElementById("result-long-url");
const resultExpiresWrapper = document.getElementById("result-expires-wrapper");
const resultExpiresAt = document.getElementById("result-expires-at");
const copyBtn = document.getElementById("copy-btn");

createForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    createErrorAlert.style.display = "none";
    createErrorAlert.textContent = "";
    createResultBox.style.display = "none";

    const longUrl = document.getElementById("long-url").value.trim();
    const customAlias = document.getElementById("custom-alias").value.trim() || null;
    const expiresAtRaw = document.getElementById("expires-at").value;

    let expiresAt = null;
    if (expiresAtRaw) {
        // Convert local datetime to ISO string
        expiresAt = new Date(expiresAtRaw).toISOString();
    }

    const payload = { long_url: longUrl };
    if (customAlias) payload.custom_alias = customAlias;
    if (expiresAt) payload.expires_at = expiresAt;

    createSubmitBtn.disabled = true;
    createSubmitBtn.textContent = "Shortening...";

    try {
        const res = await fetch("/api/links", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!res.ok) {
            const errorMsg = await parseErrorMessage(res);
            createErrorAlert.textContent = errorMsg;
            createErrorAlert.style.display = "block";
            return;
        }

        const data = await res.json();

        // Populate result securely using textContent
        shortUrlLink.href = data.short_url;
        shortUrlLink.textContent = data.short_url;
        resultLongUrl.textContent = data.long_url;

        if (data.expires_at) {
            resultExpiresAt.textContent = formatDate(data.expires_at);
            resultExpiresWrapper.style.display = "block";
        } else {
            resultExpiresWrapper.style.display = "none";
        }

        createResultBox.style.display = "block";

    } catch (err) {
        createErrorAlert.textContent = `Network Error: ${err.message || "Failed to reach server."}`;
        createErrorAlert.style.display = "block";
    } finally {
        createSubmitBtn.disabled = false;
        createSubmitBtn.textContent = "Shorten Link";
    }
});

// Copy to clipboard with user feedback
copyBtn.addEventListener("click", async () => {
    const textToCopy = shortUrlLink.textContent;
    if (!textToCopy) return;

    try {
        await navigator.clipboard.writeText(textToCopy);
        const originalText = copyBtn.textContent;
        copyBtn.textContent = "Copied!";
        copyBtn.classList.add("btn-success");
        setTimeout(() => {
            copyBtn.textContent = originalText;
            copyBtn.classList.remove("btn-success");
        }, 2000);
    } catch {
        // Fallback for older browsers
        const textarea = document.createElement("textarea");
        textarea.value = textToCopy;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
        copyBtn.textContent = "Copied!";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
    }
});

// -------------------------------------------------------------
// 2. Link Analytics View Handler
// -------------------------------------------------------------
const statsForm = document.getElementById("stats-form");
const statsSubmitBtn = document.getElementById("stats-submit-btn");
const statsCodeInput = document.getElementById("stats-code");
const statsErrorAlert = document.getElementById("stats-error-alert");
const statsDetails = document.getElementById("stats-details");

const metricTotalClicks = document.getElementById("metric-total-clicks");
const metricCode = document.getElementById("metric-code");
const metricCreatedAt = document.getElementById("metric-created-at");
const metricLongUrl = document.getElementById("metric-long-url");

statsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    statsErrorAlert.style.display = "none";
    statsErrorAlert.textContent = "";
    statsDetails.style.display = "none";

    let rawCode = statsCodeInput.value.trim();
    if (!rawCode) return;

    // If user pasted a full URL like http://localhost:8000/mycode, extract code
    if (rawCode.includes("/")) {
        const parts = rawCode.split("/");
        rawCode = parts[parts.length - 1];
    }

    statsSubmitBtn.disabled = true;
    statsSubmitBtn.textContent = "Loading...";

    try {
        // Fetch details (GET /api/links/{code}) and stats (GET /api/links/{code}/stats)
        const [linkRes, statsRes] = await Promise.all([
            fetch(`/api/links/${encodeURIComponent(rawCode)}`),
            fetch(`/api/links/${encodeURIComponent(rawCode)}/stats`)
        ]);

        if (!linkRes.ok) {
            const errorMsg = await parseErrorMessage(linkRes);
            statsErrorAlert.textContent = errorMsg;
            statsErrorAlert.style.display = "block";
            return;
        }

        if (!statsRes.ok) {
            const errorMsg = await parseErrorMessage(statsRes);
            statsErrorAlert.textContent = errorMsg;
            statsErrorAlert.style.display = "block";
            return;
        }

        const linkData = await linkRes.json();
        const statsData = await statsRes.json();

        // Render metrics securely via textContent
        metricTotalClicks.textContent = statsData.total_clicks.toLocaleString();
        metricCode.textContent = linkData.code;
        metricCreatedAt.textContent = formatDate(linkData.created_at);
        metricLongUrl.textContent = linkData.long_url;

        // Render Chart.js line chart
        renderChart(statsData.daily_clicks);

        statsDetails.style.display = "block";

    } catch (err) {
        statsErrorAlert.textContent = `Network Error: ${err.message || "Failed to fetch analytics."}`;
        statsErrorAlert.style.display = "block";
    } finally {
        statsSubmitBtn.disabled = false;
        statsSubmitBtn.textContent = "View Stats";
    }
});

function renderChart(dailyClicks) {
    const ctx = document.getElementById("clicksChart").getContext("2d");

    const labels = dailyClicks.map(d => {
        // Format YYYY-MM-DD as short MMM D (e.g. Oct 12)
        const parts = d.date.split("-");
        if (parts.length === 3) {
            const dateObj = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
            return dateObj.toLocaleDateString(undefined, { month: "short", day: "numeric" });
        }
        return d.date;
    });
    const counts = dailyClicks.map(d => d.clicks);

    if (clicksChartInstance) {
        clicksChartInstance.destroy();
    }

    clicksChartInstance = new Chart(ctx, {
        type: "line",
        data: {
            labels: labels,
            datasets: [{
                label: "Clicks per Day",
                data: counts,
                borderColor: "#3b82f6",
                backgroundColor: "rgba(59, 130, 246, 0.15)",
                fill: true,
                tension: 0.25,
                borderWidth: 2,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: "#60a5fa",
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    mode: "index",
                    intersect: false,
                    backgroundColor: "#1e293b",
                    titleColor: "#f8fafc",
                    bodyColor: "#93c5fd",
                    borderColor: "#334155",
                    borderWidth: 1,
                    padding: 10,
                }
            },
            scales: {
                x: {
                    grid: {
                        color: "rgba(51, 65, 85, 0.4)",
                    },
                    ticks: {
                        color: "#94a3b8",
                        maxTicksLimit: 10,
                        font: { size: 11 }
                    }
                },
                y: {
                    beginAtZero: true,
                    grid: {
                        color: "rgba(51, 65, 85, 0.4)",
                    },
                    ticks: {
                        color: "#94a3b8",
                        precision: 0,
                        font: { size: 11 }
                    }
                }
            }
        }
    });
}
