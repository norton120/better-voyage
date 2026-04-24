/* Leaflet + form glue for the better-voyage UI.
 *
 * Responsibilities:
 *
 * 1. Init the map with a reasonable view.
 * 2. Track origin + destination picks. First click → origin, second →
 *    destination, third → reset. Each pick drops a marker and fills
 *    the hidden form inputs.
 * 3. After the planner returns candidates, a "show on map" button
 *    fetches `/ui/voyages/{id}/geojson?candidate=N` and draws the
 *    polyline + navaid markers.
 *
 * Deliberately framework-free; HTMX owns the form and status polling.
 */

(function () {
  'use strict';

  const MAP_EL = document.getElementById('map');
  if (!MAP_EL) return;

  const map = L.map(MAP_EL, { zoomControl: true }).setView([38.9, -76.4], 7);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors',
    maxZoom: 18,
  }).addTo(map);

  // ---------------- origin / destination picking ----------------

  let originMarker = null;
  let destMarker = null;
  let routeLine = null;
  let navaidLayer = L.layerGroup().addTo(map);

  const hintEl = document.getElementById('pick-hint');
  const warnEl = document.getElementById('pick-warning');
  const resetBtn = document.getElementById('reset-picks');
  const submitBtn = document.getElementById('submit-btn');
  const originLat = document.getElementById('origin-lat');
  const originLon = document.getElementById('origin-lon');
  const destLat = document.getElementById('destination-lat');
  const destLon = document.getElementById('destination-lon');

  // Per-endpoint result from /charts/point. `valid = false` means the
  // server's land index classifies the pick as on-land and the submit
  // button is disabled. `valid = null` means we don't have an answer
  // yet (coverage not loaded, or request in flight) — submit stays
  // enabled because the planner will do an authoritative check.
  const pickStatus = { origin: null, destination: null };
  const ORIGIN_COLOR = '#67b7ff';
  const DEST_COLOR = '#ffb347';
  const LAND_COLOR = '#ff6b6b';

  function setHint(text) { if (hintEl) hintEl.innerHTML = text; }
  function showReset(show) { if (resetBtn) resetBtn.hidden = !show; }

  function setOrigin(latlng) {
    if (originMarker) map.removeLayer(originMarker);
    originMarker = L.marker(latlng, {
      title: 'origin',
      icon: coloredMarker(ORIGIN_COLOR, 'A'),
    }).addTo(map);
    originLat.value = latlng.lat.toFixed(5);
    originLon.value = latlng.lng.toFixed(5);
    setHint("click map to set <b>destination</b>");
    showReset(true);
    validatePick('origin', latlng);
  }

  function setDestination(latlng) {
    if (destMarker) map.removeLayer(destMarker);
    destMarker = L.marker(latlng, {
      title: 'destination',
      icon: coloredMarker(DEST_COLOR, 'B'),
    }).addTo(map);
    destLat.value = latlng.lat.toFixed(5);
    destLon.value = latlng.lng.toFixed(5);
    setHint("<b>origin &amp; destination set</b> — fill the form and plan the voyage");
    validatePick('destination', latlng);
  }

  async function validatePick(label, latlng) {
    pickStatus[label] = null;
    refreshPickGuard();
    const url =
      `/charts/point?lat=${encodeURIComponent(latlng.lat.toFixed(6))}` +
      `&lon=${encodeURIComponent(latlng.lng.toFixed(6))}`;
    let data;
    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`http ${resp.status}`);
      data = await resp.json();
    } catch (err) {
      // Transport / server fault — treat as unknown and let the
      // planner be the authority. Do not block submit on network blips.
      console.warn('charts/point failed', err);
      pickStatus[label] = { valid: null, reason: 'network' };
      refreshPickGuard();
      return;
    }
    // If the user clicked a different spot while this was in flight,
    // drop the stale response.
    const marker = label === 'origin' ? originMarker : destMarker;
    if (!marker) return;
    const cur = marker.getLatLng();
    if (cur.lat.toFixed(6) !== latlng.lat.toFixed(6) ||
        cur.lng.toFixed(6) !== latlng.lng.toFixed(6)) return;

    if (data.in_water === false) {
      pickStatus[label] = {
        valid: false,
        reason: 'on_land',
        distance_nm: data.distance_to_land_nm,
      };
      marker.setIcon(coloredMarker(
        LAND_COLOR, label === 'origin' ? 'A' : 'B',
      ));
    } else if (data.coverage_loaded === false) {
      pickStatus[label] = { valid: null, reason: 'loading' };
    } else {
      pickStatus[label] = {
        valid: true,
        depth_m: data.depth_m,
        distance_nm: data.distance_to_land_nm,
      };
      // In case a previous pick at this label was red, restore color.
      marker.setIcon(coloredMarker(
        label === 'origin' ? ORIGIN_COLOR : DEST_COLOR,
        label === 'origin' ? 'A' : 'B',
      ));
    }
    refreshPickGuard();
  }

  function refreshPickGuard() {
    const msgs = [];
    for (const label of ['origin', 'destination']) {
      const st = pickStatus[label];
      if (st && st.valid === false) {
        msgs.push(
          `<strong>${label}</strong> is on land — ` +
          `drag the marker or zoom in and click again.`
        );
      } else if (st && st.reason === 'loading') {
        msgs.push(
          `chart data still loading for <strong>${label}</strong> — ` +
          `submit will verify before routing.`
        );
      }
    }
    const blocked = Object.values(pickStatus).some(
      (s) => s && s.valid === false,
    );
    if (submitBtn) submitBtn.disabled = blocked;
    if (warnEl) {
      if (msgs.length) {
        warnEl.innerHTML = msgs.join('<br>');
        warnEl.hidden = false;
        warnEl.classList.toggle('pick-warning-error', blocked);
      } else {
        warnEl.hidden = true;
        warnEl.innerHTML = '';
        warnEl.classList.remove('pick-warning-error');
      }
    }
  }

  function coloredMarker(color, label) {
    return L.divIcon({
      html: `<div class="pin" style="background:${color}"><span>${label}</span></div>`,
      className: 'pin-wrapper',
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    });
  }

  function resetPicks() {
    if (originMarker) map.removeLayer(originMarker);
    if (destMarker) map.removeLayer(destMarker);
    originMarker = destMarker = null;
    originLat.value = originLon.value = '';
    destLat.value = destLon.value = '';
    clearRoute();
    setHint("click map to set <b>origin</b>");
    showReset(false);
    pickStatus.origin = pickStatus.destination = null;
    refreshPickGuard();
  }

  if (resetBtn) resetBtn.addEventListener('click', resetPicks);

  // Pick-mode is only active when the planner form is present. On the
  // voyage detail page (`/v/{id}`) the form inputs don't exist and we
  // should NOT treat a stray map click as "move an endpoint" — the
  // voyage is already routed (or routing) against fixed points.
  const PICK_MODE = !!originLat;

  if (PICK_MODE) {
    map.on('click', (e) => {
      if (!originMarker) {
        setOrigin(e.latlng);
      } else if (!destMarker) {
        setDestination(e.latlng);
      } else {
        // Third click — treat as "move the closer endpoint."
        const toOrigin = e.latlng.distanceTo(originMarker.getLatLng());
        const toDest = e.latlng.distanceTo(destMarker.getLatLng());
        if (toOrigin < toDest) {
          setOrigin(e.latlng);
        } else {
          setDestination(e.latlng);
        }
      }
    });
  }

  // ---------------- candidate → draw on map ----------------

  function clearRoute() {
    if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
    navaidLayer.clearLayers();
    document.querySelectorAll('.candidate.active').forEach((el) => {
      el.classList.remove('active');
    });
  }

  async function showCandidate(voyageId, rank) {
    const resp = await fetch(
      `/ui/voyages/${encodeURIComponent(voyageId)}/geojson?candidate=${rank}`
    );
    if (!resp.ok) {
      console.warn('geojson fetch failed', resp.status);
      return;
    }
    const data = await resp.json();
    clearRoute();
    if (data.primary && data.primary.length) {
      routeLine = L.polyline(data.primary, {
        color: '#67b7ff', weight: 3, opacity: 0.9,
      }).addTo(map);
      map.fitBounds(routeLine.getBounds(), { padding: [20, 20] });
    }
    (data.navaids || []).forEach((n) => {
      L.circleMarker([n.lat, n.lon], {
        radius: 5,
        color: navaidColor(n.sym),
        fillColor: navaidColor(n.sym),
        fillOpacity: 0.9,
        weight: 1,
      })
        .bindTooltip(`${n.name || n.sym}`, { direction: 'top' })
        .addTo(navaidLayer);
    });
  }

  function navaidColor(sym) {
    if (!sym) return '#9aa5b7';
    if (sym.includes('Red')) return '#ff5050';
    if (sym.includes('Green')) return '#46c46a';
    if (sym.includes('Yellow')) return '#f1c040';
    return '#e5e9ef';
  }

  // Delegate clicks from the status area (which HTMX swaps in).
  document.body.addEventListener('click', (e) => {
    const btn = e.target.closest('.show-on-map');
    if (!btn) return;
    const voyageId = btn.dataset.voyageId;
    const rank = btn.dataset.rank;
    if (!voyageId || !rank) return;
    btn.closest('.candidate')?.classList.add('active');
    showCandidate(voyageId, rank);
  });

  // ---------------- default form values ----------------

  // Pre-fill the window to "tomorrow + 3 days" so a new user can just
  // click two map points and submit.
  (function fillDefaultWindow() {
    const start = document.querySelector('input[name="start_at"]');
    const end = document.querySelector('input[name="end_at"]');
    if (!start || !end) return;
    const now = new Date();
    const t0 = new Date(now.getTime() + 24 * 3600 * 1000);
    const t1 = new Date(t0.getTime() + 3 * 24 * 3600 * 1000);
    start.value = toDatetimeLocal(t0);
    end.value = toDatetimeLocal(t1);
  })();

  function toDatetimeLocal(d) {
    const pad = (n) => String(n).padStart(2, '0');
    return (
      d.getFullYear() + '-' +
      pad(d.getMonth() + 1) + '-' +
      pad(d.getDate()) + 'T' +
      pad(d.getHours()) + ':' +
      pad(d.getMinutes())
    );
  }
})();

// Pin icon styling — injected here so there's no separate CSS file to
// load for the small bit of map chrome.
const pinStyle = document.createElement('style');
pinStyle.textContent = `
  .pin-wrapper { background: transparent; border: 0; }
  .pin {
    width: 22px; height: 22px; border-radius: 50%;
    border: 2px solid #05131f;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.5);
    display: flex; align-items: center; justify-content: center;
    color: #05131f; font-weight: 700; font-size: 11px;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  }
`;
document.head.appendChild(pinStyle);
