/* global L, supabase */
const config = window.DEPTOS_CONFIG;
const client = config ? supabase.createClient(config.supabaseUrl, config.supabaseAnonKey) : null;
const pendingSearchKey = "depto-pending-search";
let pendingSearch = null;
let polygonLayer = null;

function message(target, text) { document.getElementById(target).textContent = text; }
function value(form, key) { return form.elements[key].value.trim(); }
function numberOrNull(form, key) { const current = value(form, key); return current ? Number(current) : null; }
function selectedPortals(form) { return Array.from(form.querySelectorAll('input[name="portals"]:checked')).map((input) => input.value); }
function authMessage(error, action) { const detail = (error?.message || "").toLowerCase(); if (detail.includes("password") && detail.includes("6")) return "La contraseña debe tener al menos 6 caracteres."; if (detail.includes("already") || detail.includes("registered")) return "Ya existe una cuenta con ese email. Probá entrar."; if (detail.includes("signup") && detail.includes("disabled")) return "El registro por email está deshabilitado en Supabase."; if (detail.includes("provider")) return "Google no está habilitado todavía en Supabase."; if (detail.includes("invalid login")) return "Email o contraseña incorrectos."; return `No se pudo ${action}. Revisá la configuración de Auth en Supabase.`; }
function price(listing) { return listing.price ? `${listing.currency || ""} ${listing.price}`.trim() : "Precio sin informar"; }

function filtersFrom(form) {
  const polygon = value(form, "polygonGeojson");
  return { operation: value(form, "operation"), locations: [value(form, "locations")], min_price: numberOrNull(form, "minPrice"), max_price: numberOrNull(form, "maxPrice"), currency: value(form, "currency"), min_rooms: numberOrNull(form, "minRooms"), max_rooms: numberOrNull(form, "maxRooms"), min_m2: numberOrNull(form, "minM2"), max_m2: numberOrNull(form, "maxM2"), max_expenses: numberOrNull(form, "maxExpenses"), pets_allowed: form.elements.petsAllowed.checked || null, parking: form.elements.parking.checked || null, polygon_geojson: polygon ? JSON.parse(polygon) : null };
}

function renderPreview(listings) {
  const root = document.getElementById("preview-results"); root.replaceChildren();
  listings.forEach((listing) => {
    const card = document.createElement("article"); card.className = "listing";
    const portal = document.createElement("span"); portal.className = "portal-tag"; portal.textContent = listing.source; card.append(portal);
    const title = document.createElement("h3"); title.textContent = listing.title; card.append(title);
    const meta = document.createElement("p"); meta.textContent = [price(listing), listing.address, listing.rooms ? `${listing.rooms} amb.` : null, listing.covered_m2 ? `${listing.covered_m2} m²` : null].filter(Boolean).join(" · "); card.append(meta);
    const link = document.createElement("a"); link.href = listing.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Ver aviso"; card.append(link);
    root.append(card);
  });
}

function storePendingSearch(search) { pendingSearch = search; sessionStorage.setItem(pendingSearchKey, JSON.stringify(search)); }
function restorePendingSearch() { try { pendingSearch = JSON.parse(sessionStorage.getItem(pendingSearchKey) || "null"); } catch { sessionStorage.removeItem(pendingSearchKey); pendingSearch = null; } }

function initialiseMap() {
  if (!window.L) return message("preview-message", "No se pudo cargar el mapa; igual podés buscar por localidad.");
  const map = L.map("search-map").setView([-34.6037, -58.3816], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
  map.pm.addControls({ position: "topleft", drawMarker: false, drawCircleMarker: false, drawPolyline: false, drawRectangle: false, drawText: false, drawCircle: false, drawPolygon: true, editMode: true, dragMode: false, cutPolygon: false, removalMode: true, rotateMode: false });
  const syncPolygon = () => { document.getElementById("preview-form").elements.polygonGeojson.value = polygonLayer ? JSON.stringify(polygonLayer.toGeoJSON().geometry) : ""; };
  map.on("pm:create", (event) => { if (polygonLayer) map.removeLayer(polygonLayer); polygonLayer = event.layer; polygonLayer.on("pm:edit", syncPolygon); polygonLayer.on("pm:dragend", syncPolygon); syncPolygon(); });
  map.on("pm:remove", (event) => { if (event.layer === polygonLayer) { polygonLayer = null; syncPolygon(); } });
  document.getElementById("clear-polygon").onclick = () => { if (polygonLayer) map.removeLayer(polygonLayer); polygonLayer = null; syncPolygon(); };
}

function setupPreview() {
  document.getElementById("preview-form").onsubmit = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const portals = selectedPortals(form);
    if (!portals.length) return message("preview-message", "Elegí al menos un portal.");
    let filters; try { filters = filtersFrom(form); } catch { return message("preview-message", "No se pudo leer el polígono dibujado."); }
    const button = document.getElementById("preview-submit"); button.disabled = true; message("preview-message", "Buscando avisos en los portales elegidos…"); renderPreview([]);
    try {
      const response = await fetch("/api/preview", { method: "POST", body: JSON.stringify({ portals, filters }) });
      const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error();
      storePendingSearch({ filters, sourceUrls: data.source_urls }); renderPreview(data.listings);
      const unavailable = data.unavailable_portals.length ? ` Sin respuesta: ${data.unavailable_portals.join(", ")}.` : "";
      message("preview-message", data.found ? `Encontré ${data.found} avisos únicos.${unavailable}` : `No encontré avisos con esos filtros.${unavailable}`);
    } catch { message("preview-message", "No se pudo ejecutar la búsqueda ahora. Probá de nuevo más tarde."); }
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

async function showApp(session) { document.getElementById("auth").hidden = Boolean(session); document.getElementById("app").hidden = !session; if (session) await loadSearches(); }

async function setup() {
  restorePendingSearch(); initialiseMap(); setupPreview();
  if (!client) return message("auth-message", "Para guardar búsquedas, configurá Supabase.");
  const { data: { session } } = await client.auth.getSession(); await showApp(session);
  client.auth.onAuthStateChange((_event, current) => { showApp(current); });
  document.getElementById("email-login").onclick = async () => { const { error } = await client.auth.signInWithPassword({ email: document.getElementById("email").value.trim(), password: document.getElementById("password").value }); message("auth-message", error ? authMessage(error, "iniciar sesión") : "Sesión iniciada."); };
  document.getElementById("email-signup").onclick = async () => { const { error } = await client.auth.signUp({ email: document.getElementById("email").value.trim(), password: document.getElementById("password").value }); message("auth-message", error ? authMessage(error, "crear la cuenta") : "Revisá tu email para confirmar la cuenta."); };
  document.getElementById("google-login").onclick = async () => { const { error } = await client.auth.signInWithOAuth({ provider: "google", options: { redirectTo: window.location.origin } }); if (error) message("auth-message", authMessage(error, "continuar con Google")); };
  document.getElementById("sign-out").onclick = () => client.auth.signOut();
  document.getElementById("save-form").onsubmit = async (event) => {
    event.preventDefault(); if (!pendingSearch) return message("form-message", "Primero ejecutá una búsqueda para guardar sus filtros.");
    const { data: { user } } = await client.auth.getUser(); if (!user) return message("form-message", "Tu sesión venció. Volvé a ingresar.");
    const form = event.currentTarget; const { error } = await client.from("saved_searches").insert({ user_id: user.id, name: value(form, "name"), filters: pendingSearch.filters, source_urls: pendingSearch.sourceUrls, notify_chat_id: value(form, "chatId") || null });
    if (error) return message("form-message", "No se pudo guardar la búsqueda."); form.reset(); message("form-message", "Búsqueda guardada. Las novedades llegan en la próxima corrida."); await loadSearches();
  };
}
setup();
