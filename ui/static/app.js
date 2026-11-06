"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  current: null,
  images: { front: null, nutrition: null },
  contributeConfigured: false,
};

const SOURCE_LABELS = {
  openfoodfacts: "Open Food Facts",
  demo: "Demo offline",
  manual: "JSON manuale",
};

const COMPONENT_LABELS = {
  nutrition: "Nutrienti",
  additives: "Additivi",
  processing: "Lavorazione",
  organic: "Biologico",
};

const NUTRIENTS = [
  ["kcal", "Energia", "kcal"],
  ["sugars", "Zuccheri", "g"],
  ["saturated_fat", "Grassi saturi", "g"],
  ["salt", "Sale", "g"],
  ["fiber", "Fibre", "g"],
  ["protein", "Proteine", "g"],
  ["fat", "Grassi", "g"],
  ["sodium", "Sodio", "g"],
  ["fruits_veg_nuts", "Frutta/verdura", "%"],
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/d";
  return Number(value).toLocaleString("it-IT", { maximumFractionDigits: digits });
}

function pct(value) {
  if (value === null || value === undefined) return "n/d";
  return `${Math.round(Number(value) * 100)}%`;
}

function scoreColor(score) {
  if (score >= 75) return "#4ade80";
  if (score >= 50) return "#a3e635";
  if (score >= 25) return "#fbbf24";
  return "#f87171";
}

function scoreProfile(score) {
  if (score >= 75) {
    return { label: "Eccellente", tone: "great", text: "profilo nutrizionale favorevole e pochi punti critici." };
  }
  if (score >= 50) {
    return { label: "Buono", tone: "good", text: "profilo equilibrato, con qualche aspetto da tenere d'occhio." };
  }
  if (score >= 25) {
    return { label: "Scarso", tone: "warn", text: "diversi fattori pesano sul risultato: leggi le componenti e le allerte." };
  }
  return { label: "Pessimo", tone: "bad", text: "il prodotto accumula criticità importanti: controlla additivi, nutrienti e lavorazione." };
}

function riskClass(risk) {
  if (risk >= 70) return "high";
  if (risk >= 45) return "mid";
  return "low";
}

function setStatus(message, kind = "") {
  const box = $("status");
  box.textContent = message || "";
  box.className = `status ${kind}`.trim();
  box.classList.toggle("hidden", !message);
}

function setLoading(loading, label = "Analisi…") {
  $("scan-button").disabled = loading;
  $("demo-button").disabled = loading;
  $("camera-button").disabled = loading;
  $("json-button").disabled = loading;
  $("json-demo").disabled = loading;
  $("contribute-submit").disabled = loading || !state.contributeConfigured;
  $("scan-button").textContent = loading ? label : "Analizza";
  document.body.classList.toggle("loading", loading);
}

function showError(error) {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, "error");
  $("result").classList.add("hidden");
}

async function requestJSON(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
  } catch (error) {
    throw new Error("Server non raggiungibile. Avvia `python ui/server.py` e ricarica.");
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(`Risposta non valida dal server (HTTP ${response.status}).`);
  }
  if (!response.ok || !payload.ok) {
    const error = new Error(payload.error || `Errore HTTP ${response.status}.`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function renderBadges(product) {
  const badges = $("product-badges");
  badges.replaceChildren();
  const grade = product.nutriscore_grade ? String(product.nutriscore_grade).toLowerCase() : "";
  if (grade) {
    badges.append(el("span", `badge ns-${grade}`, `Nutri-Score ${grade.toUpperCase()}`));
  }
  if (product.nova_group) {
    badges.append(el("span", `badge nova-${product.nova_group}`, `NOVA ${product.nova_group}`));
  }
  if (product.organic) badges.append(el("span", "badge bio", "Biologico"));
  badges.append(el("span", "badge ghost", product.is_beverage ? "Bevanda" : "Solido"));
}

function renderComponents(components) {
  const container = $("components");
  container.replaceChildren();
  for (const component of components) {
    const item = el("div", "component");
    const head = el("div", "component-head");
    head.append(el("span", "component-label", COMPONENT_LABELS[component.name] || component.name));
    const value = component.score === null || component.score === undefined
      ? "n/d"
      : fmt(component.score, 1);
    head.append(el("strong", "component-score", value));
    item.append(head);

    const bar = el("div", "bar");
    const fill = el("span");
    fill.style.width = component.score === null || component.score === undefined
      ? "0%"
      : `${Math.max(0, Math.min(100, Number(component.score)))}%`;
    fill.style.background = component.score === null || component.score === undefined
      ? "var(--line)"
      : scoreColor(Number(component.score));
    bar.append(fill);
    item.append(bar);

    const details = component.details || {};
    let note = `peso ${Math.round(Number(component.weight || 0) * 100)}%`;
    if (component.name === "nutrition" && details.nutri_score !== undefined && details.nutri_score !== null) {
      note += ` · Nutri-Score ${fmt(details.nutri_score, 0)}`;
    } else if (component.name === "additives") {
      note += ` · ${details.count || 0} additivi · peggiore ${details.worst_additive || "—"}`;
    } else if (component.name === "processing") {
      note += details.nova_group ? ` · NOVA ${details.nova_group}` : " · NOVA non dichiarato";
    } else if (component.name === "organic") {
      note += details.organic ? " · certificato" : " · non dichiarato";
    }
    item.append(el("small", "component-note", note));
    container.append(item);
  }
}

function renderAlerts(alerts) {
  const list = $("alerts");
  list.replaceChildren();
  if (!alerts || !alerts.length) {
    list.append(el("li", "empty", "Nessuna allerta."));
    return;
  }
  for (const alert of alerts) {
    const item = el("li", `alert ${alert.level || "info"}`);
    item.append(el("strong", "alert-level", alert.level || "info"));
    item.append(el("span", "", alert.message));
    list.append(item);
  }
}

function renderReasons(reasons) {
  const list = $("reasons");
  list.replaceChildren();
  if (!reasons || !reasons.length) {
    list.append(el("li", "empty", "Nessuna spiegazione disponibile."));
    return;
  }
  for (const reason of reasons) list.append(el("li", "", reason));
}

function renderAdditives(additives) {
  const body = $("additives-body");
  body.replaceChildren();
  $("additives-count").textContent = additives && additives.length ? `(${additives.length})` : "";
  $("no-additives").classList.toggle("hidden", Boolean(additives && additives.length));
  for (const additive of additives || []) {
    const row = document.createElement("tr");
    row.append(el("td", "e-number", additive.e_number));

    const nameCell = el("td");
    nameCell.append(el("strong", "", additive.name || additive.e_number));
    if (additive.classes && additive.classes.length) {
      nameCell.append(el("small", "classes", additive.classes.join(" · ")));
    }
    if (additive.risk_estimated) nameCell.append(el("span", "estimate", "stima"));
    row.append(nameCell);

    const riskCell = el("td");
    const risk = Number(additive.risk);
    riskCell.append(el("span", `risk ${riskClass(risk)}`, `${fmt(risk, 0)}/100`));
    row.append(riskCell);

    row.append(el(
      "td",
      "dose",
      additive.dose_percent === null || additive.dose_percent === undefined
        ? "—"
        : `${fmt(additive.dose_percent, 2)}%`,
    ));
    row.append(el("td", "why", additive.why || "—"));
    body.append(row);
  }
}

function renderNutrients(nutrients) {
  const list = $("nutrients");
  list.replaceChildren();
  for (const [key, label, unit] of NUTRIENTS) {
    const wrapper = el("div", "nutrient");
    wrapper.append(el("dt", "", label));
    const value = nutrients ? nutrients[key] : null;
    const missing = value === null || value === undefined;
    wrapper.append(el("dd", missing ? "muted" : "", missing ? "n/d" : `${fmt(value, 2)} ${unit}`));
    list.append(wrapper);
  }
}

function renderResult(payload) {
  const result = payload.result;
  const product = result.product || {};

  $("product-kind").textContent = SOURCE_LABELS[payload.source] || "Prodotto";
  $("product-name").textContent = product.name || "(senza nome)";
  $("product-brand").textContent = product.brand || "Marca non dichiarata";
  $("product-barcode").textContent = product.barcode ? `EAN ${product.barcode}` : "Barcode n/d";
  $("product-quantity").textContent = product.quantity || "";
  renderBadges(product);
  state.current = payload;

  const image = $("product-image");
  if (product.image_url) {
    image.src = product.image_url;
    image.classList.remove("hidden");
    image.onerror = () => image.classList.add("hidden");
  } else {
    image.classList.add("hidden");
    image.removeAttribute("src");
  }

  $("off-product-link").href = product.barcode
    ? `https://world.openfoodfacts.org/product/${encodeURIComponent(product.barcode)}`
    : "https://world.openfoodfacts.org/";

  const ring = $("score-ring");
  ring.style.setProperty("--score-angle", `${Number(result.score) * 3.6}deg`);
  ring.style.setProperty("--score-color", scoreColor(Number(result.score)));
  $("score-value").textContent = result.score;
  $("score-grade").textContent = String(result.grade || "").toUpperCase();
  const profile = scoreProfile(Number(result.score));
  const summary = $("score-summary");
  summary.textContent = `${profile.label}: ${profile.text}`;
  summary.className = `score-summary ${profile.tone}`;

  $("confidence").textContent = pct(result.confidence);
  $("additive-coverage").textContent = pct(result.additive_coverage);
  $("result-version").textContent = result.engine_version || "—";

  renderComponents(result.components || []);
  renderAlerts(result.alerts || []);
  renderReasons(result.reasons || []);
  renderAdditives(result.additives || []);
  renderNutrients(product.nutriments || {});
  $("ingredients").textContent = product.ingredients_text || "Non dichiarati in Open Food Facts.";

  const missing = [];
  if (payload.source === "openfoodfacts") {
    if (!product.image_url) missing.push("foto del prodotto");
    if (!product.image_nutrition_url) missing.push("tabella nutrizionale");
    const nutrients = product.nutriments || {};
    if ([nutrients.kcal, nutrients.sugars, nutrients.saturated_fat, nutrients.salt].some(
      (value) => value === null || value === undefined,
    )) {
      missing.push("nutrienti");
    }
  }
  const warning = $("data-warning");
  if (missing.length) {
    $("data-warning-text").textContent = `Mancano: ${missing.join(", ")}. Aggiungi una foto per contribuire a Open Food Facts.`;
    warning.classList.remove("hidden");
  } else {
    warning.classList.add("hidden");
  }

  $("result").classList.remove("hidden");
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function setContributeStatus(message, kind = "") {
  const box = $("contribute-status");
  box.textContent = message || "";
  box.className = `status ${kind}`.trim();
  box.classList.toggle("hidden", !message);
}

function openContribute(barcode) {
  const code = barcode || (state.current && state.current.result.product.barcode) || $("barcode").value || "";
  $("contribute-barcode").value = String(code).replace(/\D/g, "").slice(0, 14);
  if (state.contributeConfigured) {
    setContributeStatus("", "");
  } else {
    setContributeStatus("Credenziali OFF non configurate sul server: imposta OFF_USER_ID e OFF_PASSWORD.", "error");
  }
  const dialog = $("contribute-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeContribute() {
  const dialog = $("contribute-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function loadBitmap(file) {
  if ("createImageBitmap" in window) return createImageBitmap(file);
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Immagine non valida.")); };
    image.src = url;
  });
}

async function prepareImage(file) {
  if (!file.type.startsWith("image/")) throw new Error("Il file non è un'immagine.");
  const source = await loadBitmap(file);
  const maxSide = 1800;
  const scale = Math.min(1, maxSide / Math.max(source.width, source.height));
  const width = Math.round(source.width * scale);
  const height = Math.round(source.height * scale);
  if (width < 640 || height < 160) {
    throw new Error("Foto troppo piccola: servono almeno 640×160 px.");
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.drawImage(source, 0, 0, width, height);
  const dataUrl = canvas.toDataURL("image/jpeg", 0.86);
  return {
    dataUrl,
    base64: dataUrl.split(",")[1],
    content_type: "image/jpeg",
    width,
    height,
    name: file.name || "photo.jpg",
  };
}

function bindPhotoSlot(kind, inputId, previewId, placeholderId) {
  const input = $(inputId);
  input.addEventListener("change", async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const prepared = await prepareImage(file);
      state.images[kind] = prepared;
      const preview = $(previewId);
      preview.src = prepared.dataUrl;
      preview.classList.remove("hidden");
      $(placeholderId).classList.add("hidden");
      input.closest(".photo-slot").classList.add("has-image");
      setContributeStatus(
        `${kind === "front" ? "Foto prodotto" : "Tabella nutrizionale"} pronta (${prepared.width}×${prepared.height}).`,
        "ok",
      );
    } catch (error) {
      input.value = "";
      setContributeStatus(error.message, "error");
    }
  });
}

async function detectBarcode(file) {
  if (!file) throw new Error("Nessuna immagine selezionata.");
  if (!("BarcodeDetector" in window)) {
    throw new Error("Questo browser non supporta la scansione automatica: inserisci il barcode a mano.");
  }
  const formats = ["ean_13", "ean_8", "upc_a", "upc_e", "qr_code"];
  let supported = formats;
  if (BarcodeDetector.getSupportedFormats) {
    const available = await BarcodeDetector.getSupportedFormats();
    supported = formats.filter((format) => available.includes(format));
  }
  if (!supported.length) throw new Error("Nessun formato barcode supportato da questo browser.");
  const detector = new BarcodeDetector({ formats: supported });
  const bitmap = await loadBitmap(file);
  const codes = await detector.detect(bitmap);
  if (!codes.length) throw new Error("Nessun barcode/QR riconosciuto. Riprova più vicino e con buona luce.");
  const barcode = String(codes[0].rawValue || "").replace(/\D/g, "");
  if (!/^\d{8,14}$/.test(barcode)) throw new Error("Il codice letto non sembra un barcode prodotto.");
  return barcode;
}

async function handleScanPhoto(file) {
  if (!file) return;
  try {
    const barcode = await detectBarcode(file);
    $("contribute-barcode").value = barcode;
    setContributeStatus(`Barcode letto: ${barcode}`, "ok");
  } catch (error) {
    setContributeStatus(error.message || "Scansione non riuscita.", "error");
  }
}

async function handleProductScan(file) {
  if (!file) return;
  setStatus("Leggo il barcode dalla foto…", "info");
  try {
    const barcode = await detectBarcode(file);
    $("barcode").value = barcode;
    await scanBarcode(barcode);
  } catch (error) {
    setStatus(error.message || "Scansione non riuscita.", "error");
  }
}

async function submitContribution() {
  const barcode = $("contribute-barcode").value.replace(/\D/g, "");
  if (!/^\d{8,14}$/.test(barcode)) {
    setContributeStatus("Inserisci un barcode di 8-14 cifre.", "error");
    return;
  }
  const images = {};
  for (const key of ["front", "nutrition"]) {
    if (state.images[key]) {
      images[key] = { data: state.images[key].base64, content_type: state.images[key].content_type };
    }
  }
  if (!Object.keys(images).length) {
    setContributeStatus("Aggiungi almeno una foto.", "error");
    return;
  }
  if (!state.contributeConfigured) {
    setContributeStatus("Credenziali OFF non configurate sul server.", "error");
    return;
  }
  setLoading(true, "Invio…");
  setContributeStatus("Invio le foto a Open Food Facts…", "info");
  try {
    const payload = await requestJSON("/api/contribute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ barcode, images }),
    });
    setContributeStatus(payload.message || "Foto inviate a Open Food Facts.", "ok");
    $("data-warning").classList.add("hidden");
  } catch (error) {
    const partial = error.payload && error.payload.uploaded && error.payload.uploaded.length;
    if (partial) {
      setContributeStatus(
        `${error.payload.error} Caricate: ${error.payload.uploaded.map((item) => item.imagefield).join(", ")}.`,
        "error",
      );
    } else {
      setContributeStatus(error.message, "error");
    }
  } finally {
    setLoading(false);
  }
}

async function scanBarcode(value) {
  const barcode = String(value || "").replace(/[\s-]/g, "");
  if (!/^\d{8,14}$/.test(barcode)) {
    showError(new Error("Il barcode deve avere tra 8 e 14 cifre."));
    return;
  }
  setLoading(true, "Scarico…");
  setStatus("Interrogo Open Food Facts…", "info");
  try {
    const payload = await requestJSON(`/api/score?barcode=${encodeURIComponent(barcode)}`);
    renderResult(payload);
    setStatus(`Analizzato su Open Food Facts · ${new Date().toLocaleTimeString("it-IT")}`, "ok");
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

async function runDemo() {
  setLoading(true, "Demo…");
  setStatus("Calcolo il prodotto demo offline…", "info");
  try {
    const payload = await requestJSON("/api/demo");
    renderResult(payload);
    setStatus("Demo offline · nessuna rete", "ok");
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

async function runManual() {
  const text = $("json-input").value.trim();
  if (!text) {
    showError(new Error("Incolla un JSON o premi «Carica demo»."));
    return;
  }
  let raw;
  try {
    raw = JSON.parse(text);
  } catch (error) {
    showError(new Error(`JSON non valido: ${error.message}`));
    return;
  }
  setLoading(true, "Calcolo…");
  setStatus("Valuto il JSON manuale…", "info");
  try {
    const payload = await requestJSON("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(raw),
    });
    renderResult(payload);
    setStatus("JSON manuale valutato", "ok");
  } catch (error) {
    showError(error);
  } finally {
    setLoading(false);
  }
}

async function loadDemoJson() {
  try {
    const payload = await requestJSON("/api/demo");
    $("json-input").value = JSON.stringify(payload.product, null, 2);
    setStatus("JSON demo caricato: premi «Valuta JSON».", "ok");
  } catch (error) {
    showError(error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("scan-form").addEventListener("submit", (event) => {
    event.preventDefault();
    scanBarcode($("barcode").value);
  });
  $("demo-button").addEventListener("click", runDemo);
  $("camera-button").addEventListener("click", () => $("camera-file").click());
  $("camera-file").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    handleProductScan(file);
    event.target.value = "";
  });
  $("json-button").addEventListener("click", runManual);
  $("json-demo").addEventListener("click", loadDemoJson);

  document.querySelectorAll("[data-barcode]").forEach((button) => {
    button.addEventListener("click", () => {
      const barcode = button.getAttribute("data-barcode");
      $("barcode").value = barcode;
      scanBarcode(barcode);
    });
  });

  $("contribute-button").addEventListener("click", () => openContribute());
  $("contribute-close").addEventListener("click", closeContribute);
  $("data-warning-button").addEventListener("click", () => openContribute());
  $("scan-barcode-button").addEventListener("click", () => $("scan-barcode-file").click());
  $("scan-barcode-file").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    handleScanPhoto(file);
    event.target.value = "";
  });
  bindPhotoSlot("front", "front-file", "front-preview", "front-placeholder");
  bindPhotoSlot("nutrition", "nutrition-file", "nutrition-preview", "nutrition-placeholder");
  $("contribute-submit").addEventListener("click", submitContribution);
  $("contribute-dialog").addEventListener("click", (event) => {
    if (event.target === $("contribute-dialog")) closeContribute();
  });

  requestJSON("/api/health")
    .then((health) => {
      const total = health.additives && health.additives.total;
      $("engine-version").textContent = `engine ${health.engine_version}${total ? ` · ${total} additivi` : ""}`;
      const configured = Boolean(health.contribute && health.contribute.configured);
      state.contributeConfigured = configured;
      $("off-credentials-note").textContent = configured
        ? "Account Open Food Facts configurato sul server."
        : "Per inviare le foto imposta OFF_USER_ID e OFF_PASSWORD nell'ambiente del server (non nel browser).";
      $("off-credentials-note").classList.toggle("warn", !configured);
      $("contribute-submit").disabled = !configured;
    })
    .catch(() => {
      $("engine-version").textContent = "engine offline";
      $("off-credentials-note").textContent = "Server non raggiungibile: impossibile verificare le credenziali OFF.";
    });

  const params = new URLSearchParams(window.location.search);
  const initial = params.get("barcode");
  if (initial) {
    $("barcode").value = initial;
    scanBarcode(initial);
  }
});
