/* global supabase */
const config = window.DEPTOS_CONFIG;
const client = config ? supabase.createClient(config.supabaseUrl, config.supabaseAnonKey) : null;
const portalHosts = { argenprop: "argenprop.com", zonaprop: "zonaprop.com.ar", mercadolibre: "mercadolibre.com.ar", remax: "remax.com.ar" };
const pendingPreviewKey = "depto-pending-preview";

function message(target, text) { document.getElementById(target).textContent = text; }
function allowedUrl(portal, value) { try { const url = new URL(value); return url.protocol === "https:" && (url.hostname === portalHosts[portal] || url.hostname.endsWith(`.${portalHosts[portal]}`)); } catch { return false; } }
function value(form, key) { return form.elements[key].value.trim(); }
function numberOrNull(form, key) { const current = value(form, key); return current ? Number(current) : null; }
function polygonOrNull(form) { const raw = value(form, "polygonGeojson"); if (!raw) return null; try { const polygon = JSON.parse(raw); if (polygon.type !== "Polygon" && polygon.type !== "MultiPolygon") throw new Error(); return polygon; } catch { throw new Error("La zona debe ser GeoJSON Polygon o MultiPolygon válido."); } }
function authMessage(error, action) { const detail = (error?.message || "").toLowerCase(); if (detail.includes("password") && detail.includes("6")) return "La contraseña debe tener al menos 6 caracteres."; if (detail.includes("already") || detail.includes("registered")) return "Ya existe una cuenta con ese email. Probá entrar."; if (detail.includes("signup") && detail.includes("disabled")) return "El registro por email está deshabilitado en Supabase."; if (detail.includes("provider")) return "Google no está habilitado todavía en Supabase."; if (detail.includes("invalid login")) return "Email o contraseña incorrectos."; return `No se pudo ${action}. Revisá la configuración de Auth en Supabase.`; }
function price(listing) { return listing.price ? `${listing.currency || ""} ${listing.price}`.trim() : "Precio sin informar"; }

function renderPreview(listings) {
  const root = document.getElementById("preview-results"); root.replaceChildren();
  listings.forEach((listing) => {
    const card = document.createElement("article"); card.className = "listing";
    const title = document.createElement("h3"); title.textContent = listing.title; card.append(title);
    const meta = document.createElement("p"); meta.textContent = [price(listing), listing.address, listing.rooms ? `${listing.rooms} amb.` : null].filter(Boolean).join(" · "); card.append(meta);
    const link = document.createElement("a"); link.href = listing.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Ver aviso"; card.append(link);
    root.append(card);
  });
}

function applyPendingPreview() {
  const raw = sessionStorage.getItem(pendingPreviewKey); if (!raw) return;
  sessionStorage.removeItem(pendingPreviewKey);
  try {
    const pending = JSON.parse(raw); const form = document.getElementById("search-form");
    if (pending && portalHosts[pending.portal] && allowedUrl(pending.portal, pending.url)) form.elements[pending.portal].value = pending.url;
  } catch { sessionStorage.removeItem(pendingPreviewKey); }
}

function setupPreview() {
  document.getElementById("preview-form").onsubmit = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const portal = value(form, "portal"); const url = value(form, "url");
    if (!allowedUrl(portal, url)) return message("preview-message", "La URL debe usar HTTPS y corresponder al portal elegido.");
    const button = document.getElementById("preview-submit"); button.disabled = true; message("preview-message", "Consultando avisos…"); renderPreview([]);
    try {
      const response = await fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ portal, url }) });
      const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.message || "No se pudo consultar el portal ahora. Probá de nuevo más tarde.");
      sessionStorage.setItem(pendingPreviewKey, JSON.stringify({ portal, url })); renderPreview(data.listings);
      message("preview-message", data.found ? `Encontré ${data.found} avisos. Mostrando hasta 12; podés iniciar sesión para guardar esta URL.` : "No encontré avisos públicos en esa página. Probá otra URL de resultados.");
    } catch (error) { message("preview-message", "No se pudo consultar el portal ahora. Probá de nuevo más tarde."); }
    finally { button.disabled = false; }
  };
}

async function loadSearches() {
  const { data, error } = await client.from("saved_searches").select("id,name,filters,created_at,listing_groups(title,address,last_price,currency,updated_at)").order("created_at", { ascending: false });
  if (error) return message("form-message", "No se pudieron cargar tus búsquedas.");
  const root = document.getElementById("searches"); root.replaceChildren();
  data.forEach((search) => {
    const card = document.createElement("article"); card.className = "search";
    const title = document.createElement("h3"); title.textContent = search.name; card.append(title);
    const details = document.createElement("p"); details.textContent = `${search.listing_groups.length} inmuebles agrupados`; card.append(details);
    search.listing_groups.slice(0, 5).forEach((listing) => { const item = document.createElement("p"); item.textContent = `${listing.title} — ${listing.currency || ""} ${listing.last_price || "s/d"}`; card.append(item); });
    root.append(card);
  });
}

async function showApp(session) { document.getElementById("auth").hidden = Boolean(session); document.getElementById("app").hidden = !session; if (session) { applyPendingPreview(); await loadSearches(); } }

async function setup() {
  setupPreview();
  if (!client) return message("auth-message", "La prueba en vivo está disponible. Para guardar búsquedas, configurá Supabase.");
  const { data: { session } } = await client.auth.getSession(); await showApp(session);
  client.auth.onAuthStateChange((_event, current) => { showApp(current); });
  document.getElementById("email-login").onclick = async () => { const { error } = await client.auth.signInWithPassword({ email: document.getElementById("email").value.trim(), password: document.getElementById("password").value }); message("auth-message", error ? authMessage(error, "iniciar sesión") : "Sesión iniciada."); };
  document.getElementById("email-signup").onclick = async () => { const { error } = await client.auth.signUp({ email: document.getElementById("email").value.trim(), password: document.getElementById("password").value }); message("auth-message", error ? authMessage(error, "crear la cuenta") : "Revisá tu email para confirmar la cuenta."); };
  document.getElementById("google-login").onclick = async () => { const { error } = await client.auth.signInWithOAuth({ provider: "google", options: { redirectTo: window.location.origin } }); if (error) message("auth-message", authMessage(error, "continuar con Google")); };
  document.getElementById("sign-out").onclick = () => client.auth.signOut();
  document.getElementById("search-form").onsubmit = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const source_urls = [];
    for (const portal of Object.keys(portalHosts)) { const url = value(form, portal); if (url) { if (!allowedUrl(portal, url)) return message("form-message", "Una URL no corresponde a su portal o no usa HTTPS."); source_urls.push({ portal, url }); } }
    if (!source_urls.length) return message("form-message", "Indicá al menos una URL de búsqueda válida.");
    const { data: { user } } = await client.auth.getUser();
    let polygon_geojson; try { polygon_geojson = polygonOrNull(form); } catch (error) { return message("form-message", error.message); }
    const filters = { operation: value(form, "operation"), locations: value(form, "locations").split(",").map((item) => item.trim()).filter(Boolean), max_price: numberOrNull(form, "maxPrice"), currency: value(form, "currency"), min_rooms: numberOrNull(form, "minRooms"), max_expenses: numberOrNull(form, "maxExpenses"), pets_allowed: form.elements.petsAllowed.checked || null, parking: form.elements.parking.checked || null, radius_km: numberOrNull(form, "radiusKm"), center_lat: numberOrNull(form, "centerLat"), center_lng: numberOrNull(form, "centerLng"), polygon_geojson };
    const { error } = await client.from("saved_searches").insert({ user_id: user.id, name: value(form, "name"), filters, source_urls, notify_chat_id: value(form, "chatId") || null });
    if (error) return message("form-message", "No se pudo guardar la búsqueda."); form.reset(); message("form-message", "Búsqueda guardada."); await loadSearches();
  };
}
setup();
