/* global supabase */
const config = window.DEPTOS_CONFIG;
const client = config ? supabase.createClient(config.supabaseUrl, config.supabaseAnonKey) : null;
const portalHosts = { argenprop: "argenprop.com", zonaprop: "zonaprop.com.ar", mercadolibre: "mercadolibre.com.ar", remax: "remax.com.ar" };

function message(target, text) { document.getElementById(target).textContent = text; }
function allowedUrl(portal, value) { try { const url = new URL(value); return url.protocol === "https:" && (url.hostname === portalHosts[portal] || url.hostname.endsWith(`.${portalHosts[portal]}`)); } catch { return false; } }
function value(form, key) { return form.elements[key].value.trim(); }
function numberOrNull(form, key) { const current = value(form, key); return current ? Number(current) : null; }
function polygonOrNull(form) { const raw = value(form, "polygonGeojson"); if (!raw) return null; try { const polygon = JSON.parse(raw); if (polygon.type !== "Polygon" && polygon.type !== "MultiPolygon") throw new Error(); return polygon; } catch { throw new Error("La zona debe ser GeoJSON Polygon o MultiPolygon válido."); } }

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
  if (!client) return message("auth-message", "Copiá config.example.js a config.js y completá Supabase.");
  const { data: { session } } = await client.auth.getSession(); await showApp(session);
  client.auth.onAuthStateChange((_event, current) => { showApp(current); });
  document.getElementById("email-login").onclick = async () => { const { error } = await client.auth.signInWithPassword({ email: document.getElementById("email").value.trim(), password: document.getElementById("password").value }); message("auth-message", error ? "No se pudo iniciar sesión." : "Sesión iniciada."); };
  document.getElementById("email-signup").onclick = async () => { const { error } = await client.auth.signUp({ email: document.getElementById("email").value.trim(), password: document.getElementById("password").value }); message("auth-message", error ? "No se pudo crear la cuenta." : "Revisá tu email para confirmar la cuenta."); };
  document.getElementById("google-login").onclick = () => client.auth.signInWithOAuth({ provider: "google", options: { redirectTo: window.location.origin } });
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
