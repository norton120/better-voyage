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
  const resetBtn = document.getElementById('reset-picks');
  const originLat = document.getElementById('origin-lat');
  const originLon = document.getElementById('origin-lon');
  const destLat = document.getElementById('destination-lat');
  const destLon = document.getElementById('destination-lon');

  function setHint(text) { if (hintEl) hintEl.innerHTML = text; }
  function showReset(show) { if (resetBtn) resetBtn.hidden = !show; }

  function setOrigin(latlng) {
    if (originMarker) map.removeLayer(originMarker);
    originMarker = L.marker(latlng, {
      title: 'origin',
      icon: coloredMarker('#67b7ff', 'A'),
    }).addTo(map);
    originLat.value = latlng.lat.toFixed(5);
    originLon.value = latlng.lng.toFixed(5);
    setHint("click map to set <b>destination</b>");
    showReset(true);
  }

  function setDestination(latlng) {
    if (destMarker) map.removeLayer(destMarker);
    destMarker = L.marker(latlng, {
      title: 'destination',
      icon: coloredMarker('#ffb347', 'B'),
    }).addTo(map);
    destLat.value = latlng.lat.toFixed(5);
    destLon.value = latlng.lng.toFixed(5);
    setHint("<b>origin &amp; destination set</b> — fill the form and plan the voyage");
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
  }

  if (resetBtn) resetBtn.addEventListener('click', resetPicks);

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
