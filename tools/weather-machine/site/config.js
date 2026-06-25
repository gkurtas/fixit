// config.js — all tunable settings for the Philadelphia storm feed.
// Kept separate from app.js so a non-programmer can adjust scope, cadence, and
// styling without touching application logic.

const CONFIG = {
  // --- Data sources -------------------------------------------------------
  // NWS active alerts. We query four states (the PHI office straddles all of
  // them) and then filter client-side to the Mount Holly office. Querying by
  // state is the documented, stable way to get a small payload; the alerts
  // endpoint has no reliable "by office" filter for ACTIVE alerts.
  alertsUrl: 'https://api.weather.gov/alerts/active?area=PA,NJ,DE,MD',

  // Iowa Environmental Mesonet Local Storm Reports, GeoJSON.
  // NOTE: this endpoint ignores ?wfo=, so we request a time window only and
  // filter to the PHI office client-side (see app.js).
  lsrBaseUrl: 'https://mesonet.agron.iastate.edu/geojson/lsr.php',

  // Storm-report window, in hours. Drives BOTH the request (?hours=) and the
  // client-side prune, so a stalled poll never shows reports older than this.
  lsrWindowHours: 24,

  // The PHI / Mount Holly forecast office. Alerts carry this exact senderName;
  // LSR features carry wfo === 'PHI'. These two strings define our geographic
  // scope. Change them (and the map center) to retarget another metro.
  alertSenderName: 'NWS Mount Holly NJ',
  lsrWfo: 'PHI',

  // --- Polling cadence (milliseconds) ------------------------------------
  // Alerts update near-real-time (API cache-control is 5s); 60s is courteous
  // and plenty fast for a newsroom feed. IEM refreshes LSRs ~every 5 minutes.
  alertsRefreshMs: 60 * 1000,
  lsrRefreshMs: 5 * 60 * 1000,

  // How often to re-apply time filters WITHOUT re-fetching, so an expired
  // alert disappears on schedule (and 24h-old reports drop) even if the next
  // poll is delayed or fails.
  pruneMs: 30 * 1000,

  // --- Enriched feed (server-side plain-language rewrites) ----------------
  // The rewrite pipeline writes this file (locally next to the page, in
  // production to the S3 bucket). The page reads it instead of calling the
  // live APIs, and falls back to the live APIs if it is missing or older than
  // feedStaleMs. Demo mode (?demo=1) ignores it and uses the bundled fixtures.
  feedUrl: 'feed.json',
  feedRefreshMs: 60 * 1000,
  feedStaleMs: 20 * 60 * 1000,

  // --- Radar overlay (RainViewer — free, no API key) ---------------------
  // A precipitation layer drawn beneath the alert polygons and report dots.
  // The page reads RainViewer's frame manifest for the newest radar image and
  // refreshes on a timer. The on/off choice is remembered per browser.
  radar: {
    enabled: true,            // shown by default; the toggle overrides + persists
    manifestUrl: 'https://api.rainviewer.com/public/weather-maps.json',
    refreshMs: 4 * 60 * 1000, // national radar updates roughly every 5 minutes
    opacity: 0.6,
    size: 256,
    color: 6,                 // RainViewer palette (6 = NEXRAD Level III, US-familiar)
    smooth: 1,                // 1 = smoothed tiles
    snow: 1,                  // 1 = render snow distinctly
    maxNativeZoom: 7,         // RainViewer's free radar tops out at z7; Leaflet upscales above
  },

  // --- Map ---------------------------------------------------------------
  mapCenter: [39.95, -75.17], // Philadelphia City Hall
  mapZoom: 8,

  // --- Severity ranking (NWS CAP severity values) ------------------------
  // Used to sort the feed and color badges/polygons. Higher = more severe.
  severityRank: { Extreme: 5, Severe: 4, Moderate: 3, Minor: 2, Unknown: 1 },

  // Colorblind-safe severity palette (ColorBrewer / viridis-adjacent).
  // Verified ≥4.5:1 contrast against white badge text in style.css.
  severityColor: {
    Extreme: '#6a1b9a', // deep purple
    Severe: '#c62828', // red
    Moderate: '#d97706', // amber (yellow-leaning, to separate clearly from Severe red)
    Minor: '#1565c0', // blue
    Unknown: '#546e7a', // slate
  },

  // --- Storm report categories -------------------------------------------
  // IEM LSR `typetext` is free-ish text; we bucket it for icons/colors.
  // Buckets are matched by keyword in order; first match wins.
  lsrCategories: [
    { key: 'tornado', label: 'Tornado', color: '#6a1b9a', match: ['TORNADO', 'FUNNEL', 'WATERSPOUT'] },
    { key: 'flood', label: 'Flooding', color: '#1565c0', match: ['FLOOD', 'FLASH FLOOD'] },
    { key: 'hail', label: 'Hail', color: '#00838f', match: ['HAIL'] },
    { key: 'wind', label: 'Wind', color: '#c62828', match: ['TSTM WND', 'WIND', 'GST', 'GUST', 'MARINE'] },
    { key: 'snow', label: 'Snow/Ice', color: '#455a64', match: ['SNOW', 'ICE', 'SLEET', 'FREEZING'] },
    { key: 'rain', label: 'Rain', color: '#2e7d32', match: ['RAIN', 'PRECIP'] },
  ],
  lsrDefault: { key: 'other', label: 'Other', color: '#546e7a' },
};
