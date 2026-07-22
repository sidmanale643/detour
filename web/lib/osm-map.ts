import type { StyleSpecification } from "maplibre-gl";

const tileUrl = process.env.NEXT_PUBLIC_OSM_TILE_URL || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const gameMapTilejsonUrl = process.env.NEXT_PUBLIC_GAME_MAP_TILEJSON_URL || "https://tiles.openfreemap.org/planet";

export const osmRasterStyle: StyleSpecification = {
  version: 8,
  sources: {
    openstreetmap: {
      type: "raster",
      tiles: [tileUrl],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
      maxzoom: 19,
    },
  },
  layers: [{ id: "openstreetmap", type: "raster", source: "openstreetmap" }],
};

/**
 * The Map tab's compact, label-free game world. The source is configurable so
 * production can move to a hosted or self-hosted OpenMapTiles-compatible feed
 * without changing the game presentation. Keep `osmRasterStyle` for the home
 * area picker, where a conventional map remains more useful.
 */
export const gameMapStyle: StyleSpecification = {
  version: 8,
  sources: {
    "detour-world": {
      type: "vector",
      url: gameMapTilejsonUrl,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [
    {
      id: "parchment-ground",
      type: "background",
      paint: { "background-color": "#f4ecdd" },
    },
    {
      id: "peach-open-land",
      type: "fill",
      source: "detour-world",
      "source-layer": "landuse",
      paint: { "fill-color": "#ecd9cd", "fill-opacity": 0.38 },
    },
    {
      id: "muted-teal-parks",
      type: "fill",
      source: "detour-world",
      "source-layer": "park",
      paint: { "fill-color": "#a7c9bd", "fill-opacity": 0.94 },
    },
    {
      id: "aqua-water",
      type: "fill",
      source: "detour-world",
      "source-layer": "water",
      paint: { "fill-color": "#83d7d7", "fill-opacity": 0.96 },
    },
    {
      id: "lavender-building-footprints",
      type: "fill",
      source: "detour-world",
      "source-layer": "building",
      maxzoom: 16,
      paint: { "fill-color": "#bcb2ce", "fill-opacity": 0.3 },
    },
    {
      id: "diorama-buildings",
      type: "fill-extrusion",
      source: "detour-world",
      "source-layer": "building",
      minzoom: 16,
      paint: {
        "fill-extrusion-color": "#c3b5d1",
        "fill-extrusion-opacity": 0.42,
        "fill-extrusion-height": ["*", 0.38, ["coalesce", ["get", "render_height"], ["get", "height"], 5]],
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], ["get", "min_height"], 0],
      },
    },
    {
      id: "major-road-casing",
      type: "line",
      source: "detour-world",
      "source-layer": "transportation",
      filter: ["match", ["get", "class"], ["motorway", "trunk", "primary", "secondary", "tertiary"], true, false],
      paint: {
        "line-color": "#24345a",
        "line-opacity": 0.22,
        "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1, 14, 3.5, 17, 8],
      },
    },
    {
      id: "major-roads",
      type: "line",
      source: "detour-world",
      "source-layer": "transportation",
      filter: ["match", ["get", "class"], ["motorway", "trunk", "primary", "secondary", "tertiary"], true, false],
      paint: {
        "line-color": "#fff9eb",
        "line-width": ["interpolate", ["linear"], ["zoom"], 10, 0.6, 14, 2.2, 17, 5.7],
      },
    },
    {
      id: "local-streets",
      type: "line",
      source: "detour-world",
      "source-layer": "transportation",
      filter: ["match", ["get", "class"], ["minor", "service"], true, false],
      paint: { "line-color": "#fff9eb", "line-opacity": 0.9, "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.45, 16, 1.8, 18, 3.2] },
    },
    {
      id: "paths-and-tracks",
      type: "line",
      source: "detour-world",
      "source-layer": "transportation",
      filter: ["match", ["get", "class"], ["track", "path"], true, false],
      paint: { "line-color": "#6ba995", "line-opacity": 0.9, "line-width": ["interpolate", ["linear"], ["zoom"], 14, 0.5, 17, 1.6], "line-dasharray": [1.2, 1.2] },
    },
  ],
};

export const osrmBaseUrl = (process.env.NEXT_PUBLIC_OSRM_BASE_URL || "https://routing.openstreetmap.de/routed-foot").replace(/\/$/, "");
