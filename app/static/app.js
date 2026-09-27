/* Village Pond Planner - frontend
 * Leaflet map + Leaflet.draw for parcels + Chart.js for rainfall charts.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n, d = 0) => (n === null || n === undefined || Number.isNaN(Number(n))) ? "-" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: 0 });

  const COLORS = {
    catchment: "#2f80ed", catchmentFill: "#2f80ed",
    drainage: "#00b8ff", pond: "#ff3d71", pondFill: "#ff8fab",
    preferred: "#18a058", suitable: "#f2c94c", farmland: "#f2994a",
    parcel: "#b14cff", area: "#ffffff",
    contour: "#8a5a2b", contourMajor: "#5b3510",
  };
  const COVER_COLORS = { water: "#1e78c8", built_up: "#dc3c3c", vegetation: "#3caa3c", open_land: "#e6be50", forest: "#1d6b3a" };
  const COVER_NAMES = { water: "Water", built_up: "Built-up", vegetation: "Vegetation / crops", open_land: "Open / fallow", forest: "Forest" };

  // ------------------------------------------------------------------
  // Map
  // ------------------------------------------------------------------
  const map = L.map("map", { zoomControl: true }).setView([20.6, 78.9], 5);
  const base = {
    "Satellite (Esri)": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics", maxZoom: 19 }),
    "Topographic (OpenTopoMap)": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)", maxZoom: 17, subdomains: "abc" }),
    "Streets (OSM)": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors", maxZoom: 19 }),
  };
  if (window.MAPTILER_KEY) {
    base["Satellite hybrid (MapTiler)"] = L.tileLayer("https://api.maptiler.com/maps/hybrid/{z}/{x}/{y}.jpg?key=" + window.MAPTILER_KEY, {
      tileSize: 512, zoomOffset: -1, maxZoom: 20, attribution: "&copy; MapTiler &copy; OpenStreetMap contributors" });
  }
  base["Satellite (Esri)"].addTo(map);
  // Place labels on top of the satellite imagery
  const labels = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19, zIndex: 5 });
  labels.addTo(map);

  const overlays = {};
  const layerControl = L.control.layers(base, { "Place labels": labels }, { position: "topright", collapsed: true }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);

  const parcels = new L.FeatureGroup().addTo(map);
  layerControl.addOverlay(parcels, "Government land parcels");

  let centerMarker = null, pondPin = null, areaRect = null;
  let selected = null;         // {lat, lon, name}
  let lastResult = null;
  let charts = {};

  // Legend
  const legend = L.control({ position: "bottomright" });
  legend.onAdd = function () {
    const d = L.DomUtil.create("div", "legend");
    d.innerHTML =
      "<b>Legend</b>" +
      `<div><i style="background:${COLORS.pondFill};border:2px solid ${COLORS.pond}"></i>Recommended pond</div>` +
      `<div><i style="background:${COLORS.catchment}55;border:2px solid ${COLORS.catchment}"></i>Catchment</div>` +
      `<div><i style="background:${COLORS.drainage};height:3px"></i>Drainage lines</div>` +
      `<div><i style="background:${COLORS.contour};height:2px"></i>Contours</div>` +
      `<div><i style="background:${COLORS.preferred}88"></i>Preferred land (public/common)</div>` +
      `<div><i style="background:${COLORS.suitable}88"></i>Suitable open land</div>` +
      `<div><i style="background:${COLORS.farmland}88"></i>Farmland (private, farm pond)</div>` +
      `<div><i style="background:${COLORS.parcel}55;border:2px solid ${COLORS.parcel}"></i>Government parcels</div>`;
    L.DomEvent.disableClickPropagation(d);
    return d;
  };
  legend.addTo(map);

  // Drawing government parcels
  const drawHandler = new L.Draw.Polygon(map, {
    allowIntersection: false, showArea: true, metric: true,
    shapeOptions: { color: COLORS.parcel, weight: 2, fillOpacity: 0.2 },
  });
  map.on(L.Draw.Event.CREATED, (e) => {
    e.layer.setStyle({ color: COLORS.parcel, weight: 2, fillOpacity: 0.2 });
    parcels.addLayer(e.layer);
    $("drawParcelBtn").classList.remove("active");
    updateParcelInfo();
  });
  map.on(L.Draw.Event.DRAWSTOP, () => $("drawParcelBtn").classList.remove("active"));

  $("drawParcelBtn").addEventListener("click", () => {
    $("drawParcelBtn").classList.add("active");
    drawHandler.enable();
  });
  $("clearParcelsBtn").addEventListener("click", () => { parcels.clearLayers(); updateParcelInfo(); });
  $("parcelFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const gj = JSON.parse(await f.text());
      const layer = L.geoJSON(gj, { style: { color: COLORS.parcel, weight: 2, fillOpacity: 0.2 } });
      let n = 0;
      layer.eachLayer((l) => { if (l instanceof L.Polygon) { parcels.addLayer(l); n++; } });
      if (!n) throw new Error("no polygons found");
      map.fitBounds(parcels.getBounds(), { padding: [30, 30] });
      updateParcelInfo();
    } catch (err) {
      setStatus("Could not read parcels: " + err.message, "error");
    }
    e.target.value = "";
  });

  function parcelsGeoJSON() {
    const n = parcels.getLayers().length;
    return n ? parcels.toGeoJSON() : null;
  }
  function updateParcelInfo() {
    const n = parcels.getLayers().length;
    let area = 0;
    parcels.eachLayer((l) => { if (L.GeometryUtil && l.getLatLngs) area += L.GeometryUtil.geodesicArea(l.getLatLngs()[0]); });
    $("parcelInfo").textContent = n
      ? `${n} parcel${n > 1 ? "s" : ""}, ${fmt(area / 10000, 2)} ha. The pond will be sited only inside these parcels.`
      : "Without parcels, available land is inferred from satellite imagery and OpenStreetMap.";
  }

  // ------------------------------------------------------------------
  // Tabs
  // ------------------------------------------------------------------
  document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => { b.classList.toggle("active", b === btn); b.setAttribute("aria-selected", String(b === btn)); });
    ["village", "contour", "history"].forEach((t) => { $("tab-" + t).hidden = btn.dataset.tab !== t; });
    if (btn.dataset.tab === "history") loadHistory();
  }));

  // ------------------------------------------------------------------
  // Status
  // ------------------------------------------------------------------
  function setStatus(msg, kind, busy) {
    const el = $("status");
    el.innerHTML = (busy ? '<span class="spinner"></span>' : "") + esc(msg);
    el.className = "status show " + kind;
  }
  function clearStatus() { $("status").className = "status"; }

  // ------------------------------------------------------------------
  // Village search / selection
  // ------------------------------------------------------------------
  async function doSearch() {
    const q = $("searchInput").value.trim();
    if (q.length < 2) return;
    const ul = $("searchResults");
    ul.innerHTML = '<li class="hint">Searching...</li>';
    try {
      const r = await fetch("/api/geocode?q=" + encodeURIComponent(q));
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.statusText);
      if (!data.results.length) { ul.innerHTML = '<li class="hint">No matches in India. Try another spelling or click the map.</li>'; return; }
      ul.innerHTML = "";
      data.results.forEach((p) => {
        const li = document.createElement("li");
        li.innerHTML = `${esc(p.name)} <small>${esc([p.type, p.district, p.state].filter(Boolean).join(" · "))}</small>`;
        li.tabIndex = 0;
        li.setAttribute("role", "button");
        li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); li.click(); } });
        li.addEventListener("click", () => {
          ul.innerHTML = "";
          selectLocation(p.lat, p.lon, p.name, [p.district, p.state].filter(Boolean).join(", "));
          map.setView([p.lat, p.lon], 14);
        });
        ul.appendChild(li);
      });
    } catch (err) {
      ul.innerHTML = `<li class="hint">Search failed: ${esc(err.message)}</li>`;
    }
  }
  $("searchBtn").addEventListener("click", doSearch);
  $("searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSearch(); } });

  function selectLocation(lat, lon, name, region) {
    selected = { lat, lon, name: name || null };
    if (centerMarker) centerMarker.setLatLng([lat, lon]);
    else centerMarker = L.circleMarker([lat, lon], { radius: 6, color: "#fff", weight: 2, fillColor: "#1c6fd1", fillOpacity: 1 }).addTo(map);
    drawArea();
    $("selectedPlace").innerHTML = `<b>${esc(name || "Selected point")}</b>${region ? ", " + esc(region) : ""}<br>` +
      `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    $("runVillageBtn").disabled = false;
  }

  function drawArea() {
    if (!selected) return;
    const r = Number($("radius").value);
    const dLat = r / 111320, dLon = r / (111320 * Math.cos(selected.lat * Math.PI / 180));
    const b = [[selected.lat - dLat, selected.lon - dLon], [selected.lat + dLat, selected.lon + dLon]];
    if (areaRect) areaRect.setBounds(b);
    else areaRect = L.rectangle(b, { color: COLORS.area, weight: 1.5, dashArray: "6 5", fill: false, interactive: false }).addTo(map);
  }
  $("radius").addEventListener("change", drawArea);

  $("manualPond").addEventListener("change", (e) => {
    $("manualHint").hidden = !e.target.checked;
    if (!e.target.checked && pondPin) { map.removeLayer(pondPin); pondPin = null; }
  });

  let popupJustClosed = 0;
  map.on("popupclose", () => { popupJustClosed = Date.now(); });
  map.on("click", async (e) => {
    if (drawHandler.enabled() || Date.now() - popupJustClosed < 300) return;
    const { lat, lng } = e.latlng;
    if ($("manualPond").checked && selected) {
      if (pondPin) pondPin.setLatLng(e.latlng);
      else pondPin = L.marker(e.latlng, { draggable: true, title: "Chosen pond location" }).addTo(map);
      pondPin.bindTooltip("Chosen pond location").openTooltip();
      return;
    }
    if (!$("tab-village").hidden) {
      selectLocation(lat, lng, null, null);
      try {
        const r = await fetch(`/api/reverse?lat=${lat}&lon=${lng}`);
        if (r.ok) {
          const p = await r.json();
          if (p && p.name && selected && selected.lat === lat) {
            selectLocation(lat, lng, p.name, [p.district, p.state].filter(Boolean).join(", "));
          }
        }
      } catch (_) { /* name is optional */ }
    }
  });

  // ------------------------------------------------------------------
  // Run analyses
  // ------------------------------------------------------------------
  const STAGES = ["Fetching elevation tiles", "Classifying satellite imagery", "Querying OpenStreetMap",
    "Routing surface flow", "Retrieving 20 years of CHIRPS rainfall (up to a minute on first run)", "Designing the pond"];
  let stageTimer = null;
  function showStage(i) {
    $("stageList").innerHTML = STAGES.map((s, k) =>
      `<li class="${k < i ? "done" : k === i ? "current" : ""}">${esc(s)}</li>`).join("");
    setStatus(STAGES[i] + "...", "info", true);
  }
  function startStages() {
    let i = 0;
    $("mapOverlay").hidden = false;
    showStage(0);
    stageTimer = setInterval(() => { i = Math.min(i + 1, STAGES.length - 2); showStage(i); }, 1600);
  }
  function stopStages() { clearInterval(stageTimer); stageTimer = null; $("mapOverlay").hidden = true; }

  $("runVillageBtn").addEventListener("click", async () => {
    if (!selected) return;
    const body = {
      lat: selected.lat, lon: selected.lon, village: selected.name,
      radius_m: Number($("radius").value),
      grid_resolution: Number($("gridRes").value),
      soil_group: $("soilGroup").value,
      rainfall_years: Number($("years").value),
      rainfall_source: $("rainSource").value,
      annual_normal_mm: $("rainNormal").value ? Number($("rainNormal").value) : null,
      fill_fraction: Number($("fillFraction").value),
      max_depth_m: Number($("maxDepth").value),
      side_slope: Number($("sideSlope").value),
      max_slope_deg: Number($("maxSlope").value),
      land_parcels: parcelsGeoJSON(),
    };
    if ($("manualPond").checked && pondPin) {
      const p = pondPin.getLatLng();
      body.pond_lat = p.lat; body.pond_lon = p.lng;
    }
    await runRequest($("runVillageBtn"), () => fetch("/api/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }));
  });

  $("contourForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("fileInput").files[0];
    if (!file) { setStatus("Choose a KML or KMZ file first.", "error"); return; }
    const form = new FormData();
    form.append("file", file);
    const pg = parcelsGeoJSON();
    if (pg) form.append("land_parcels", JSON.stringify(pg));
    const params = new URLSearchParams({
      soil_group: $("cSoil").value, grid_resolution: $("cGrid").value,
      annual_rainfall_mm: $("cRain").value, dam_height_m: $("cDam").value,
      analyze_land: $("cLand").checked, fetch_rainfall: $("cRainApi").checked,
      village: file.name.replace(/\.(kml|kmz)$/i, ""),
    });
    await runRequest($("runContourBtn"), () => fetch("/analyzeContour?" + params, { method: "POST", body: form }));
  });

  async function runRequest(btn, doFetch) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Analysing...";
    startStages();
    const t0 = performance.now();
    try {
      const r = await doFetch();
      const data = await r.json();
      stopStages();
      if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText);
      render(data);
      setStatus(`Done in ${((performance.now() - t0) / 1000).toFixed(1)} s. Saved as analysis #${data.analysis_id ?? "-"}.`, "success");
    } catch (err) {
      stopStages();
      setStatus("Analysis failed: " + err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ------------------------------------------------------------------
  // Render results
  // ------------------------------------------------------------------
  function stat(label, value, unit, hero) {
    return `<div class="stat${hero ? " hero" : ""}"><div class="label">${esc(label)}</div>` +
      `<div class="value">${value}${unit ? ` <span class="unit">${esc(unit)}</span>` : ""}</div></div>`;
  }

  function clearOverlays() {
    Object.values(overlays).forEach((l) => { map.removeLayer(l); layerControl.removeLayer(l); });
    for (const k in overlays) delete overlays[k];
  }
  function addOverlay(name, layer, visible) {
    overlays[name] = layer;
    layerControl.addOverlay(layer, name);
    if (visible) layer.addTo(map);
  }

  let lastBase = null, currentIdx = 0;
  function render(base, idx = 0, switching = false) {
    lastBase = base;
    currentIdx = idx;
    const cands = base.candidates && base.candidates.length ? base.candidates : null;
    const cand = cands ? cands[Math.min(idx, cands.length - 1)] : null;
    const d = cand ? {
      ...base, pond_site: cand.pond_site, catchment: cand.catchment, runoff: cand.runoff,
      pond_design: cand.pond_design, layers: { ...base.layers, pond_footprint: cand.pond_footprint },
    } : base;
    lastResult = d;
    clearOverlays();
    const L_ = d.layers || {};
    const site = d.pond_site, pd = d.pond_design, c = d.catchment, ro = d.runoff, rain = d.rainfall, land = d.land;

    // --- map layers (added bottom to top) ---
    if (L_.elevation_png) addOverlay("Elevation relief", L.imageOverlay(L_.elevation_png, L_.bounds, { opacity: 0.6 }), false);
    if (L_.landcover_png) addOverlay("Land-cover classification", L.imageOverlay(L_.landcover_png, L_.bounds, { opacity: 0.55, className: "pixelated" }), false);
    if (L_.suitable_land) {
      addOverlay("Available land", L.geoJSON(L_.suitable_land, {
        style: (f) => ({ stroke: f.properties.tier !== "suitable", color: COLORS[f.properties.tier], weight: 1, fillColor: COLORS[f.properties.tier], fillOpacity: f.properties.tier === "suitable" ? 0.14 : 0.35 }),
        bubblingMouseEvents: false,
        onEachFeature: (f, l) => l.bindTooltip(`${f.properties.tier} land, ${fmt(f.properties.area_m2 / 10000, 2)} ha`, { sticky: true }),
      }), true);
    }
    if (L_.contours) {
      const iv = L_.contours.properties?.interval_m;
      addOverlay(`Contours${iv ? ` (${iv} m)` : ""}`, L.geoJSON(L_.contours, {
        style: (f) => ({ color: f.properties.major ? COLORS.contourMajor : COLORS.contour, weight: f.properties.major ? 1.6 : 0.8, opacity: 0.9 }),
        onEachFeature: (f, l) => l.bindTooltip(`${fmt(f.properties.elevation_m, 1)} m`, { sticky: true, className: "contour-label" }),
      }), true);
    }
    const catchLayer = L.geoJSON(c.boundary_geojson, {
      style: { color: COLORS.catchment, weight: 2.5, fillColor: COLORS.catchmentFill, fillOpacity: 0.12 },
      bubblingMouseEvents: false,
    }).bindPopup(`<b>Catchment</b><br>${fmt(c.area_hectares, 2)} ha · relief ${fmt(c.relief_m, 1)} m`);
    addOverlay("Catchment", catchLayer, true);
    if (L_.drainage) {
      addOverlay("Drainage network", L.geoJSON(L_.drainage, {
        style: (f) => ({ color: COLORS.drainage, weight: Math.min(1 + Math.log10(1 + f.properties.upstream_area_ha), 4.5), opacity: 0.9 }),
        onEachFeature: (f, l) => l.bindTooltip(`Upstream area ${fmt(f.properties.upstream_area_ha, 1)} ha`, { sticky: true }),
      }), true);
    }
    if (L_.bounds) addOverlay("Analysis area", L.rectangle(L_.bounds, { color: "#fff", weight: 1.5, dashArray: "6 5", fill: false, interactive: false }), true);
    if (L_.pond_footprint) {
      addOverlay("Pond footprint", L.geoJSON(L_.pond_footprint, {
        style: { color: COLORS.pond, weight: 2.5, fillColor: COLORS.pondFill, fillOpacity: 0.55 },
      }), true);
    }
    if (cands && cands.length > 1) {
      const others = L.layerGroup();
      cands.forEach((k, i) => {
        if (i === idx) return;
        const m = L.marker([k.pond_site.location.lat, k.pond_site.location.lon], {
          icon: L.divIcon({ className: "", iconSize: [24, 24], iconAnchor: [12, 12],
            html: `<div class="cand-pin" aria-hidden="true">${k.rank}</div>` }),
          title: `Candidate ${k.rank}`, keyboard: true, bubblingMouseEvents: false,
        }).bindTooltip(`Candidate ${k.rank}: ${fmt(k.pond_design.storage_capacity_m3)} m³, catchment ${fmt(k.catchment.area_hectares, 0)} ha. Click to view.`);
        m.on("click", () => render(base, i, true));
        others.addLayer(m);
      });
      addOverlay("Other candidate sites", others, true);
    }
    const pondIcon = L.divIcon({
      className: "", iconSize: [26, 26], iconAnchor: [13, 13],
      html: `<div class="pond-pulse"></div><svg style="position:relative" width="26" height="26" viewBox="0 0 26 26"><circle cx="13" cy="13" r="11" fill="${COLORS.pond}" stroke="#fff" stroke-width="2.5"/><path d="M13 6c-2 3-4.5 5.6-4.5 8a4.5 4.5 0 0 0 9 0C17.5 11.6 15 9 13 6Z" fill="#fff"/></svg>`,
    });
    const marker = L.marker([site.location.lat, site.location.lon], { icon: pondIcon, zIndexOffset: 1000, bubblingMouseEvents: false }).bindPopup(
      `<b>${cand ? `Candidate ${cand.rank}${cand.rank === 1 ? " (best)" : ""}` : "Recommended pond"}</b><br>${site.location.lat.toFixed(6)}, ${site.location.lon.toFixed(6)}<br>` +
      `Ground level ${fmt(site.location.elevation_m, 1)} m · slope ${fmt(site.slope_deg, 1)}°<br>` +
      `${fmt(pd.top_length_m, 0)} × ${fmt(pd.top_width_m, 0)} m, ${fmt(pd.water_depth_m, 1)} m deep<br>` +
      `Storage <b>${fmt(pd.storage_capacity_m3)} m³</b><br>` +
      `Catchment ${fmt(c.area_hectares, 1)} ha · runoff ${fmt(ro.mean_annual_runoff_m3)} m³/yr` +
      (rain ? `<br>Rainfall ${fmt(rain.mean_annual_mm)} mm/yr (${esc(rain.period)})` : ""));
    addOverlay("Pond site", marker, true);
    if (pondPin) { map.removeLayer(pondPin); pondPin = null; }
    catchBounds = catchLayer.getBounds();
    pondLatLng = L.latLng(site.location.lat, site.location.lon);
    map.fitBounds(catchBounds.pad(0.25));
    setTimeout(() => marker.openPopup(), 400);

    // --- sidebar ---
    $("results").hidden = false;
    $("welcome").hidden = true;
    $("resultTitle").textContent = d.village || "Analysis results";
    $("resultSub").textContent = (cand ? `Candidate ${cand.rank} of ${cands.length} · ` : "") +
      `pond at ${site.location.lat.toFixed(5)}, ${site.location.lon.toFixed(5)}` +
      (d.analysis_id ? ` · analysis #${d.analysis_id}` : "");
    renderCandidates(cands, idx, base);
    $("kStorage").innerHTML = `${fmt(pd.storage_capacity_m3)} <span class="unit">m³</span>`;
    $("kDims").textContent = `${fmt(pd.top_length_m, 0)} m × ${fmt(pd.top_width_m, 0)} m × ${fmt(pd.water_depth_m, 1)} m deep · ` +
      `${fmt(pd.water_spread_area_m2 / 10000, 2)} ha water surface`;
    $("kCatch").innerHTML = `${fmt(c.area_hectares, c.area_hectares < 10 ? 1 : 0)} <small>ha</small>`;
    $("kRain").innerHTML = `${fmt(ro.mean_annual_rainfall_mm)} <small>mm</small>`;
    $("kRunoff").innerHTML = ro.mean_annual_runoff_m3 >= 1e6
      ? `${fmt(ro.mean_annual_runoff_m3 / 1e6, 2)} <small>M m³</small>`
      : `${fmt(ro.mean_annual_runoff_m3 / 1000, 0)}k <small>m³</small>`;
    $("kFills").innerHTML = `${fmt(pd.fills_per_dependable_year, 1)}<small>×</small>`;
    if (!switching) selectPane("pond");
    $("warnings").innerHTML = (d.warnings || []).map((w) => `<div class="warning">${esc(w)}</div>`).join("");

    $("pondStats").innerHTML =
      stat("Size at top (L × W)", `${fmt(pd.top_length_m, 0)} × ${fmt(pd.top_width_m, 0)}`, "m") +
      stat("Water depth", fmt(pd.water_depth_m, 1), `m (+${pd.freeboard_m} freeboard)`) +
      stat("Bed (L × W)", `${fmt(pd.bed_length_m, 0)} × ${fmt(pd.bed_width_m, 0)}`, "m") +
      stat("Side slope", `${pd.side_slope_h_per_v} : 1`, "H:V") +
      stat("Water spread area", fmt(pd.water_spread_area_m2 / 10000, 2), "ha") +
      stat("Earthwork (excavation)", fmt(pd.excavation_volume_m3), "m³") +
      stat("Fills per dependable year", fmt(pd.fills_per_dependable_year, 1), "×") +
      stat("Dry-season evaporation", pd.dry_season_evaporation_loss_m3 == null ? "-" : fmt(pd.dry_season_evaporation_loss_m3), pd.evaporation_loss_pct_of_storage == null ? "" : `m³ (${fmt(pd.evaporation_loss_pct_of_storage)}%)`) +
      stat("Seepage (4 months)", fmt(pd.seasonal_seepage_loss_m3), "m³");
    $("pondSection").innerHTML = crossSection(pd);
    $("pondNotes").innerHTML = [`Sized to ${esc(pd.design_basis)}.`, ...(pd.notes || []).map(esc)].map((n) => `<li>${n}</li>`).join("");

    $("siteStats").innerHTML =
      stat("Latitude, longitude", `${site.location.lat.toFixed(5)}, ${site.location.lon.toFixed(5)}`) +
      stat("Ground elevation", fmt(site.location.elevation_m, 1), "m") +
      stat("Ground slope", fmt(site.slope_deg, 2), "°") +
      stat("Land at site", site.land_tier ? site.land_tier.replace("_", " ") : "not assessed");
    $("siteReason").textContent = site.reason;

    $("catchStats").innerHTML =
      stat("Area", fmt(c.area_hectares, 2), "ha") +
      stat("Area", fmt(c.area_km2, 3), "km²") +
      stat("Perimeter", fmt(c.perimeter_m / 1000, 2), "km") +
      stat("Relief", fmt(c.relief_m, 1), "m") +
      stat("Mean slope", fmt(c.mean_slope_deg, 2), "°") +
      stat("Elevation range", `${fmt(c.elevation_min_m, 0)}–${fmt(c.elevation_max_m, 0)}`, "m");

    if (rain) {
      $("rainSourceNote").innerHTML = `${esc(rain.source)}, ${esc(rain.period)} (${rain.years_of_record} years)` +
        (rain.scaled_to_normal ? `. Scaled ×${rain.scaled_to_normal.scale_factor} to your normal of ${fmt(rain.scaled_to_normal.normal_mm)} mm (dataset mean ${fmt(rain.scaled_to_normal.dataset_mean_mm)} mm)` : "") +
        (rain.source_url ? ` · <a href="${esc(rain.source_url)}" target="_blank" rel="noopener">source</a>` : "");
      $("rainStats").innerHTML =
        stat("Mean annual rainfall", fmt(rain.mean_annual_mm), "mm", true) +
        stat("75% dependable", fmt(rain.dependable_75pct_mm), "mm") +
        stat("Variability (CV)", fmt(rain.coefficient_of_variation * 100, 0), "%") +
        stat("Driest year", `${fmt(rain.min_annual.mm)} (${rain.min_annual.year})`, "mm") +
        stat("Wettest year", `${fmt(rain.max_annual.mm)} (${rain.max_annual.year})`, "mm") +
        stat("Rainy days / year", fmt(rain.mean_rainy_days, 0), "≥2.5 mm") +
        stat("Monsoon share", fmt(rain.monsoon_jun_sep_share * 100, 0), "% Jun–Sep") +
        stat("Heaviest day", fmt(rain.max_daily_mm, 0), "mm") +
        stat("Reference ET₀", fmt(rain.mean_annual_et0_mm), "mm/yr");
    } else {
      $("rainSourceNote").textContent = "Historical rainfall not available; runoff uses the fallback rainfall value.";
      $("rainStats").innerHTML = "";
    }
    drawCharts(rain, ro);

    $("runoffStats").innerHTML =
      stat("Mean annual runoff", fmt(ro.mean_annual_runoff_m3), "m³", true) +
      stat("Dependable (75%) runoff", fmt(ro.dependable_annual_runoff_m3), "m³") +
      stat("Runoff depth", fmt(ro.mean_annual_runoff_mm, 0), "mm/yr") +
      stat("Effective runoff coeff.", fmt(ro.effective_runoff_coefficient ?? ro.rational_runoff_coefficient, 2), "") +
      stat("Curve number (AMC II)", fmt(ro.composite_curve_number, 1), `soil ${ro.hydrologic_soil_group}`) +
      stat("Rational method check", fmt(ro.rational_method_runoff_m3), `m³ (C=${fmt(ro.rational_runoff_coefficient, 2)})`);
    $("coverBar").innerHTML = coverBar(ro.catchment_landcover_share);

    if (land) {
      $("landStats").innerHTML =
        stat("Suitable land in area", fmt(land.suitable_area_m2 / 10000, 1), `ha (${fmt(land.suitable_fraction * 100, 0)}%)`) +
        stat("Preferred (public/common)", fmt(land.preferred_area_m2 / 10000, 1), "ha") +
        (land.osm_feature_counts ? stat("OSM buildings / roads", `${fmt(land.osm_feature_counts.buildings)} / ${fmt(land.osm_feature_counts.roads)}`, "") : "") +
        stat("Max ground slope", fmt(land.max_slope_deg, 0), "°");
      $("landNote").textContent = `Sources: ${(land.sources || []).join(", ").replace(/_/g, " ")}. ${land.ownership_note}`;
    } else {
      $("landStats").innerHTML = "";
      $("landNote").textContent = "Land availability was not assessed.";
    }

    const src = d.source_map || {};
    $("metaInfo").textContent = `Terrain: ${src.elevation_source || src.type || "-"} · grid ${d.dem.grid_rows}×${d.dem.grid_cols} @ ${fmt(d.dem.cell_size_m, 1)} m · ` +
      `server time ${fmt(d.processing_seconds, 1)} s`;

    if (switching) return;
    const sb = $("sidebar");
    sb.scrollTo({ top: sb.scrollTop + $("results").getBoundingClientRect().top - sb.getBoundingClientRect().top - 52, behavior: "smooth" });
  }

  let catchBounds = null, pondLatLng = null;
  function selectPane(name) {
    document.querySelectorAll(".subtab").forEach((b) => { b.classList.toggle("active", b.dataset.pane === name); b.setAttribute("aria-selected", String(b.dataset.pane === name)); });
    document.querySelectorAll(".pane").forEach((p) => { p.hidden = p.id !== "pane-" + name; });
    Object.values(charts).forEach((ch) => ch.resize());
  }
  document.querySelectorAll(".subtab").forEach((b) => b.addEventListener("click", () => selectPane(b.dataset.pane)));
  $("zoomPond").addEventListener("click", () => { if (pondLatLng) map.flyTo(pondLatLng, 17, { duration: 1.2 }); });
  $("zoomCatch").addEventListener("click", () => { if (catchBounds) map.flyToBounds(catchBounds.pad(0.25), { duration: 1.2 }); });
  $("printReport").addEventListener("click", () => window.print());
  let paneBeforePrint = null;
  window.addEventListener("beforeprint", () => {
    paneBeforePrint = document.querySelector(".subtab.active")?.dataset.pane;
    document.querySelectorAll(".pane").forEach((p) => { p.hidden = false; });
    Object.values(charts).forEach((ch) => ch.resize(360, 200));
  });
  window.addEventListener("afterprint", () => { if (paneBeforePrint) selectPane(paneBeforePrint); });

  function renderCandidates(cands, idx, base) {
    const box = $("candList");
    if (!cands || cands.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const total = cands.reduce((s, k) => s + k.pond_design.storage_capacity_m3, 0);
    box.innerHTML = `<div class="cand-head"><h3>Candidate sites</h3><span>${cands.length} sites · ${fmt(total)} m³ combined</span></div>` +
      `<table class="cand-table"><caption class="sr-only">Ranked candidate pond sites. Select a row to view it.</caption>` +
      `<thead><tr><th scope="col">#</th><th scope="col">Storage</th><th scope="col">Catchment</th><th scope="col">Size</th><th scope="col">Score</th></tr></thead><tbody>` +
      cands.map((k, i) => `<tr class="${i === idx ? "active" : ""}" tabindex="0" data-i="${i}" aria-selected="${i === idx}">` +
        `<td><span class="cand-rank">${k.rank}</span></td>` +
        `<td>${fmt(k.pond_design.storage_capacity_m3)} m³</td>` +
        `<td>${fmt(k.catchment.area_hectares, 0)} ha</td>` +
        `<td>${fmt(k.pond_design.top_length_m, 0)}×${fmt(k.pond_design.top_width_m, 0)}×${fmt(k.pond_design.water_depth_m, 1)} m</td>` +
        `<td>${k.score == null ? "-" : fmt(k.score * 100, 0)}</td></tr>`).join("") +
      `</tbody></table>`;
    box.querySelectorAll("tr[data-i]").forEach((tr) => {
      const go = () => render(base, Number(tr.dataset.i), true);
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
  }

  function coverBar(share) {
    if (!share) return "";
    const entries = Object.entries(share).filter(([, v]) => v > 0.001);
    return `<div class="cover-bar">${entries.map(([k, v]) => `<span style="width:${v * 100}%;background:${COVER_COLORS[k]}" title="${COVER_NAMES[k]} ${(v * 100).toFixed(1)}%"></span>`).join("")}</div>` +
      `<div class="cover-legend">${entries.map(([k, v]) => `<span><span class="swatch" style="background:${COVER_COLORS[k]}"></span>${COVER_NAMES[k]} ${(v * 100).toFixed(0)}%</span>`).join("")}</div>`;
  }

  function crossSection(pd) {
    // Schematic cross-section across the width, to scale horizontally vs vertically exaggerated.
    const W = 380, H = 120, pad = 30;
    const topW = pd.top_width_m + 2 * pd.side_slope_h_per_v * pd.freeboard_m;
    const sx = (W - 2 * pad) / topW;
    const depth = pd.water_depth_m + pd.freeboard_m;
    const sy = (H - 45) / depth;
    const x0 = pad, y0 = 22;
    const bedL = x0 + pd.side_slope_h_per_v * depth * sx, bedR = W - pad - pd.side_slope_h_per_v * depth * sx;
    const wl = y0 + pd.freeboard_m * sy;
    const wlL = x0 + pd.side_slope_h_per_v * pd.freeboard_m * sx, wlR = W - pad - pd.side_slope_h_per_v * pd.freeboard_m * sx;
    const yb = y0 + depth * sy;
    return `<svg class="section-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Pond cross-section">
      <line x1="0" y1="${y0}" x2="${W}" y2="${y0}" stroke="#8a6d3b" stroke-width="1.5"/>
      <polygon points="${wlL},${wl} ${wlR},${wl} ${bedR},${yb} ${bedL},${yb}" fill="#4a90d9" fill-opacity=".55"/>
      <polyline points="${x0},${y0} ${bedL},${yb} ${bedR},${yb} ${W - pad},${y0}" fill="none" stroke="#6b4f2a" stroke-width="2"/>
      <text x="${W / 2}" y="${y0 - 7}" text-anchor="middle">top ${fmt(topW, 0)} m</text>
      <text x="${W / 2}" y="${yb + 14}" text-anchor="middle">bed ${fmt(pd.bed_width_m, 0)} m</text>
      <text x="${W - pad + 4}" y="${(wl + yb) / 2 + 4}">${fmt(pd.water_depth_m, 1)} m</text>
      <text x="${wlL - 4}" y="${(y0 + yb) / 2 + 16}" text-anchor="end">${pd.side_slope_h_per_v}:1</text>
    </svg>`;
  }

  function drawCharts(rain, ro) {
    Object.values(charts).forEach((ch) => ch.destroy());
    charts = {};
    const series = rain?.annual_series || [];
    const runoffByYear = Object.fromEntries((ro.annual_series || []).map((r) => [r.year, r.runoff_mm]));
    $("annualChart").parentElement.hidden = !series.length;
    $("monthlyChart").parentElement.hidden = !rain;
    if (!series.length) return;
    const grid = { color: "rgba(0,0,0,.06)" };
    charts.annual = new Chart($("annualChart"), {
      type: "bar",
      data: {
        labels: series.map((s) => s.year),
        datasets: [
          { label: "Rainfall (mm)", data: series.map((s) => s.rainfall_mm), backgroundColor: "#6fa8dc", borderRadius: 2 },
          { label: "Runoff (mm)", data: series.map((s) => runoffByYear[s.year] ?? null), backgroundColor: "#1c4f8a", borderRadius: 2 },
          { label: "Mean rainfall", type: "line", data: series.map(() => rain.mean_annual_mm), borderColor: "#e0892b", borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 },
        ],
      },
      options: {
        maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { boxWidth: 10, font: { size: 11 } } }, title: { display: true, text: "Annual rainfall and runoff", font: { size: 12 } } },
        scales: { x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 60 } }, y: { grid, ticks: { font: { size: 10 } }, title: { display: true, text: "mm", font: { size: 10 } } } },
      },
    });
    charts.monthly = new Chart($("monthlyChart"), {
      type: "bar",
      data: { labels: rain.monthly_mean_mm.map((m) => m.month), datasets: [{ label: "Mean monthly rainfall (mm)", data: rain.monthly_mean_mm.map((m) => m.rainfall_mm), backgroundColor: "#6fa8dc", borderRadius: 2 }] },
      options: {
        maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, title: { display: true, text: "Mean monthly rainfall (mm)", font: { size: 12 } } },
        scales: { x: { grid: { display: false }, ticks: { font: { size: 10 } } }, y: { grid, ticks: { font: { size: 10 } } } },
      },
    });
  }

  // ------------------------------------------------------------------
  // Downloads
  // ------------------------------------------------------------------
  function download(name, obj) {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  const slug = () => (lastResult?.village || "analysis").replace(/[^a-z0-9]+/gi, "_").toLowerCase();
  $("dlJson").addEventListener("click", () => {
    if (!lastResult) return;
    const copy = JSON.parse(JSON.stringify(lastBase || lastResult));
    if (copy.layers) { delete copy.layers.elevation_png; delete copy.layers.landcover_png; }
    download(`${slug()}_pond_report.json`, copy);
  });
  $("dlGeo").addEventListener("click", () => {
    if (!lastResult) return;
    const d = lastResult, feats = [];
    feats.push({ type: "Feature", properties: { name: "pond_site", ...d.pond_design, notes: undefined }, geometry: { type: "Point", coordinates: [d.pond_site.location.lon, d.pond_site.location.lat] } });
    if (d.layers.pond_footprint) feats.push({ ...d.layers.pond_footprint, properties: { name: "pond_footprint" } });
    feats.push({ ...d.catchment.boundary_geojson, properties: { name: "catchment", area_ha: d.catchment.area_hectares } });
    (d.layers.suitable_land?.features || []).forEach((f) => feats.push({ ...f, properties: { name: "available_land", ...f.properties } }));
    (d.layers.drainage?.features || []).forEach((f) => feats.push({ ...f, properties: { name: "drainage", ...f.properties } }));
    download(`${slug()}_layers.geojson`, { type: "FeatureCollection", features: feats });
  });

  // ------------------------------------------------------------------
  // History
  // ------------------------------------------------------------------
  async function loadHistory() {
    const ul = $("historyList");
    ul.innerHTML = '<li class="hint">Loading...</li>';
    try {
      const r = await fetch("/api/analyses?limit=100");
      const data = await r.json();
      if (!data.analyses.length) { ul.innerHTML = '<li class="hint">No saved analyses yet.</li>'; return; }
      ul.innerHTML = "";
      data.analyses.forEach((a) => {
        const li = document.createElement("li");
        li.className = "item";
        li.tabIndex = 0;
        li.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === li) li.click(); });
        const s = a.summary || {};
        li.innerHTML = `<div><div class="title">#${a.id} ${esc(a.village || (a.mode === "contour_upload" ? "Contour upload" : "Unnamed location"))}</div>` +
          `<div class="sub">${esc(a.created_at.replace("T", " ").replace("Z", " UTC"))}</div>` +
          `<div class="sub">${fmt(s.storage_m3)} m³ pond · ${fmt(s.depth_m, 1)} m deep · catchment ${fmt(s.catchment_ha, 1)} ha</div></div>` +
          `<button class="del" title="Delete" aria-label="Delete analysis ${a.id}">&times;</button>`;
        li.addEventListener("click", async (e) => {
          if (e.target.closest(".del")) {
            e.stopPropagation();
            await fetch(`/api/analyses/${a.id}`, { method: "DELETE" });
            loadHistory();
            return;
          }
          setStatus(`Loading analysis #${a.id}...`, "info", true);
          const res = await fetch(`/api/analyses/${a.id}`);
          if (!res.ok) { setStatus("Could not load analysis.", "error"); return; }
          const full = await res.json();
          full.analysis_id = a.id;
          render(full);
          setStatus(`Loaded analysis #${a.id}.`, "success");
        });
        ul.appendChild(li);
      });
    } catch (err) {
      ul.innerHTML = `<li class="hint">Could not load history: ${esc(err.message)}</li>`;
    }
  }
  $("refreshHistory").addEventListener("click", loadHistory);
})();
