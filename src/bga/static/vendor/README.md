# Vendored front-end assets

Nothing here is fetched from a CDN. The venue may have no internet, and a
dashboard whose map silently fails to load in front of a panel is worse than
one that never claimed to have a map.

## Leaflet 1.9.4 — two files, both required

Download into **this directory**, keeping these exact filenames:

    https://unpkg.com/leaflet@1.9.4/dist/leaflet.js     ->  leaflet.js
    https://unpkg.com/leaflet@1.9.4/dist/leaflet.css    ->  leaflet.css

PowerShell:

```powershell
curl.exe -L -o src/bga/static/vendor/leaflet.js  https://unpkg.com/leaflet@1.9.4/dist/leaflet.js
curl.exe -L -o src/bga/static/vendor/leaflet.css https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
```

Expected sizes, as a rough check: `leaflet.js` about 147 KB, `leaflet.css`
about 15 KB.

## No image files are needed

Leaflet normally also wants `dist/images/marker-icon.png`, `marker-icon-2x.png`
and `marker-shadow.png` for its default pin. The map here uses `L.circleMarker`
for the collection point and the facility instead, so there are no image
requests at all and nothing to 404 when offline.

## Verifying it works offline

1. Download both files.
2. Turn off WiFi.
3. Reload a request detail page.

The OpenStreetMap tiles will not load — that is expected and fine. The radius
circle, the collection point, the facility and the ward circles must all still
draw, on the `#eef1f4` background, with no console errors. If the map area is
blank instead, `leaflet.js` is missing or misnamed.

## If you skip this

The page still renders. The map block is guarded on `window.L`, so a missing
`leaflet.js` degrades to no map rather than a broken page or a JavaScript
error. Everything else on the page is server-rendered and unaffected.
