/* global L, supabase */
const config = window.DEPTOS_CONFIG;
const client = config ? supabase.createClient(config.supabaseUrl, config.supabaseAnonKey) : null;
const pendingSearchKey = "depto-pending-search";
let pendingSearch = null;
let polygonLayer = null;
let searchMap = null;
let listingMarkers = null;
const selectedLocations = [];

function message(target, text) { document.getElementById(target).textContent = text; }
function value(form, key) { return form.elements[key].value.trim(); }
function numberOrNull(form, key) { const current = value(form, key); return current ? Number(current) : null; }
function selectedPortals(form) { return Array.from(form.querySelectorAll('input[name="portals"]:checked')).map((input) => input.value); }
function authMessage(error, action) { const detail = (error?.message || "").toLowerCase(); if (detail.includes("password") && detail.includes("6")) return "La contraseña debe tener al menos 6 caracteres."; if (detail.includes("already") || detail.includes("registered")) return "Ya existe una cuenta con ese email. Probá entrar."; if (detail.includes("signup") && detail.includes("disabled")) return "El registro por email está deshabilitado en Supabase."; if (detail.includes("provider")) return "Google no está habilitado todavía en Supabase."; if (detail.includes("invalid login")) return "Email o contraseña incorrectos."; return `No se pudo ${action}. Revisá la configuración de Auth en Supabase.`; }
function price(listing) { return listing.price ? `${listing.currency || ""} ${listing.price}`.trim() : "Precio sin informar"; }

function normalizedLocation(location) { return location.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim(); }
function renderLocationChips() { const root = document.getElementById("location-chips"); root.replaceChildren(); selectedLocations.forEach((location, index) => { const chip = document.createElement("button"); chip.type = "button"; chip.className = "location-chip"; chip.dataset.locationIndex = String(index); chip.textContent = `${location} ×`; root.append(chip); }); }
function addLocations(rawLocations) { const candidates = rawLocations.split(",").map((location) => location.trim()).filter(Boolean); for (const location of candidates) { if (location.length > 100) return "Cada localidad debe tener hasta 100 caracteres."; if (selectedLocations.some((current) => normalizedLocation(current) === normalizedLocation(location))) continue; if (selectedLocations.length === 10) return "Podés agregar hasta 10 localidades."; selectedLocations.push(location); } renderLocationChips(); return ""; }
function addPendingLocation() { const input = document.getElementById("location-input"); const error = addLocations(input.value); if (!error) input.value = ""; return error; }
function setupLocations() { const input = document.getElementById("location-input"); document.getElementById("add-location").onclick = () => { const error = addPendingLocation(); if (error) message("preview-message", error); }; input.onkeydown = (event) => { if (event.key === "Enter" || event.key === ",") { event.preventDefault(); const error = addPendingLocation(); if (error) message("preview-message", error); } }; document.getElementById("location-chips").onclick = (event) => { const button = event.target.closest("button[data-location-index]"); if (!button) return; selectedLocations.splice(Number(button.dataset.locationIndex), 1); renderLocationChips(); }; }

function filtersFrom(form) {
  const polygon = value(form, "polygonGeojson");
  return { operation: value(form, "operation"), locations: [...selectedLocations], min_price: numberOrNull(form, "minPrice"), max_price: numberOrNull(form, "maxPrice"), currency: value(form, "currency"), min_rooms: numberOrNull(form, "minRooms"), max_rooms: numberOrNull(form, "maxRooms"), min_m2: numberOrNull(form, "minM2"), max_m2: numberOrNull(form, "maxM2"), max_expenses: numberOrNull(form, "maxExpenses"), pets_allowed: form.elements.petsAllowed.checked || null, parking: form.elements.parking.checked || null, polygon_geojson: polygon ? JSON.parse(polygon) : null };
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

function mapListingContent(listing, includeLink) { const root = document.createElement("article"); root.className = "map-listing"; const title = document.createElement("strong"); title.textContent = listing.title; root.append(title); const details = document.createElement("p"); const area = listing.covered_m2 || listing.total_m2; details.textContent = [price(listing), area ? `${area} m²` : null, listing.rooms ? `${listing.rooms} amb.` : null, listing.source, listing.address].filter(Boolean).join(" · "); root.append(details); if (includeLink) { const link = document.createElement("a"); link.href = listing.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Ver aviso"; root.append(link); } return root; }
function coordinatesFor(listing) { const latitude = Number(listing.latitude); const longitude = Number(listing.longitude); return Number.isFinite(latitude) && Number.isFinite(longitude) ? [latitude, longitude] : null; }
function renderListingMarkers(listings) { if (!listingMarkers || !searchMap) return; listingMarkers.clearLayers(); const bounds = []; listings.forEach((listing) => { const coordinates = coordinatesFor(listing); if (!coordinates) return; const marker = L.circleMarker(coordinates, { radius: 8, color: "#f8fafc", weight: 2, fillColor: "#7c3aed", fillOpacity: 1, title: listing.title }); marker.bindTooltip(mapListingContent(listing, false), { direction: "top", offset: [0, -8], opacity: 1 }); marker.bindPopup(mapListingContent(listing, true), { maxWidth: 300 }); marker.addTo(listingMarkers); bounds.push(coordinates); }); document.getElementById("map-results-status").textContent = bounds.length ? `${bounds.length} aviso(s) ubicados en el mapa. Pasá el mouse por un punto para ver el detalle.` : listings.length ? "Los avisos encontrados no informan coordenadas para ubicarlos en el mapa." : ""; if (bounds.length && !polygonLayer) searchMap.fitBounds(bounds, { padding: [28, 28], maxZoom: 14 }); }

function storePendingSearch(search) { pendingSearch = search; sessionStorage.setItem(pendingSearchKey, JSON.stringify(search)); }
function restorePendingSearch() { try { pendingSearch = JSON.parse(sessionStorage.getItem(pendingSearchKey) || "null"); } catch { sessionStorage.removeItem(pendingSearchKey); pendingSearch = null; } }

function initialiseMap() {
  if (!window.L) return message("preview-message", "No se pudo cargar el mapa; igual podés buscar por localidad.");
  searchMap = L.map("search-map").setView([-34.6037, -58.3816], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(searchMap);
  listingMarkers = L.layerGroup().addTo(searchMap);
  searchMap.pm.addControls({ position: "topleft", drawMarker: false, drawCircleMarker: false, drawPolyline: false, drawRectangle: false, drawText: false, drawCircle: false, drawPolygon: true, editMode: true, dragMode: false, cutPolygon: false, removalMode: true, rotateMode: false });
  const syncPolygon = () => { document.getElementById("preview-form").elements.polygonGeojson.value = polygonLayer ? JSON.stringify(polygonLayer.toGeoJSON().geometry) : ""; };
  searchMap.on("pm:create", (event) => { if (polygonLayer) searchMap.removeLayer(polygonLayer); polygonLayer = event.layer; polygonLayer.on("pm:edit", syncPolygon); polygonLayer.on("pm:dragend", syncPolygon); syncPolygon(); });
  searchMap.on("pm:remove", (event) => { if (event.layer === polygonLayer) { polygonLayer = null; syncPolygon(); } });
  document.getElementById("clear-polygon").onclick = () => { if (polygonLayer) searchMap.removeLayer(polygonLayer); polygonLayer = null; syncPolygon(); };
}

function setupPreview() {
  document.getElementById("preview-form").onsubmit = async (event) => {
    event.preventDefault(); const form = event.currentTarget; const portals = selectedPortals(form);
    if (!portals.length) return message("preview-message", "Elegí al menos un portal.");
    const locationError = addPendingLocation(); if (locationError) return message("preview-message", locationError);
    let filters; try { filters = filtersFrom(form); } catch { return message("preview-message", "No se pudo leer el polígono dibujado."); }
    if (!filters.locations.length) return message("preview-message", "Agregá al menos una localidad o barrio.");
    if (filters.locations.length * portals.length > 20) return message("preview-message", "Reducí localidades o portales: el máximo es 20 combinaciones.");
    const button = document.getElementById("preview-submit"); button.disabled = true; message("preview-message", "Buscando avisos en los portales elegidos…"); renderPreview([]); renderListingMarkers([]);
    try {
      const response = await fetch("/api/preview", { method: "POST", body: JSON.stringify({ portals, filters }) });
      const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.error_id ? `${data.message} Código: ${data.error_id}` : data.message || "No se pudo ejecutar la búsqueda ahora.");
      storePendingSearch({ filters, sourceUrls: data.source_urls }); renderPreview(data.listings); renderListingMarkers(data.listings);
      const diagnostics = diagnosticsMessage(data.source_diagnostics || []);
      const diagnosticMessage = diagnostics ? ` Estado: ${diagnostics}.` : "";
      message("preview-message", data.found ? `Encontré ${data.found} avisos únicos.${diagnosticMessage}` : `No encontré avisos con esos filtros.${diagnosticMessage}`);
    } catch (error) { message("preview-message", error.message || "No se pudo ejecutar la búsqueda ahora. Probá de nuevo más tarde."); }
    finally { button.disabled = false; }
  };
}

function diagnosticsMessage(diagnostics) { const labels = { location: "ubicación", currency: "moneda", price_min: "precio mín.", price_max: "precio máx.", rooms_min: "ambientes mín.", rooms_max: "ambientes máx.", area_min: "m² mín.", area_max: "m² máx.", expenses: "expensas", pets: "mascotas", parking: "cochera", polygon: "polígono" }; const groups = new Map(); diagnostics.forEach((diagnostic) => { const group = groups.get(diagnostic.portal) || { total: 0, matching: 0, unavailable: 0, rejected: {} }; if (diagnostic.status === "unavailable") group.unavailable += 1; else { group.total += diagnostic.listing_count || 0; group.matching += diagnostic.matching_count || 0; Object.entries(diagnostic.rejected_by || {}).forEach(([reason, count]) => { group.rejected[reason] = (group.rejected[reason] || 0) + count; }); } groups.set(diagnostic.portal, group); }); return Array.from(groups, ([portal, group]) => { if (!group.total && group.unavailable) return `${portal}: sin respuesta`; const topRejection = Object.entries(group.rejected).sort(([, left], [, right]) => right - left)[0]; const rejected = topRejection ? `; ${topRejection[1]} descartados por ${labels[topRejection[0]] || "filtro"}` : ""; const unavailable = group.unavailable ? `; ${group.unavailable} consulta(s) sin respuesta` : ""; return `${portal}: ${group.total} extraídos, ${group.matching} con filtros${rejected}${unavailable}`; }).join(" · "); }

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
  restorePendingSearch(); initialiseMap(); setupLocations(); setupPreview();
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
