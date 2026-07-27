"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Map as MapLibreMap, Marker } from "maplibre-gl";
import { questApi, type Coordinate, type Quest } from "../lib/quest-api";
import { gameMapStyle } from "../lib/osm-map";
import styles from "./QuestMap.module.css";

export type QuestMapStatus = "offered" | "active" | "completed" | "skipped" | "superseded" | "expired";
export interface QuestMapQuest { id: string; title: string; emoji: string; status: QuestMapStatus; accent?: string; place?: string; coordinates?: readonly [longitude: number, latitude: number]; }
export interface QuestMapProps {
  quests: readonly Quest[]; activeQuest: Quest | null; onSelectQuest: (quest: Quest) => void;
  homeCenter: Coordinate | null; homeLabel?: string; level?: number; completedCount?: number; className?: string;
  dateLabel?: string; xp?: number; refreshAvailable?: boolean; onRefresh?: () => void;
  onGenerate?: () => void; generating?: boolean;
}
type RouteSummary = { distanceMeters: number; durationSeconds: number };
type EdgeIndicator = { quest: Quest; x: number; y: number; angle: number; distance: number };
type LngLat = [number, number];

const ROUTE_SOURCE = "detour-route";
const ROUTE_CASING_LAYER = "detour-route-casing";
const ROUTE_INNER_LAYER = "detour-route-line";
const GAME_CAMERA = { zoom: 16, pitch: 50, bearing: -18 } as const;
const MAP_LOAD_TIMEOUT_MS = 15_000;
/** Re-request a walking route once the player has walked this far from the last origin. */
const REROUTE_DISTANCE_METERS = 90;
/** Minimum gap between automatic re-routes (Google Routes rate / UX). */
const REROUTE_COOLDOWN_MS = 45_000;
const ACCENT_COLORS: Record<string, string> = { coral: "#ff745d", aqua: "#43d3d4", purple: "#b697f4", gold: "#ffc954", mint: "#83d6a6", blue: "#78a8f6" };

type LivePosition = { longitude: number; latitude: number; heading: number | null };

const stateIcon = (status: QuestMapStatus, emoji: string) => status === "completed" ? "✓" : status === "skipped" || status === "expired" ? "–" : emoji;
const metersText = (meters: number) => meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`;
const minutesText = (seconds: number) => `${Math.max(1, Math.round(seconds / 60))} min walk`;
const timerText = (milliseconds: number) => {
  const total = Math.max(0, Math.ceil(milliseconds / 1000));
  return `${Math.floor(total / 3600)}:${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
};

/** Great-circle distance in meters — used to decide when current location has moved enough to re-route. */
const distanceMeters = (a: LngLat, b: LngLat) => {
  const toRad = (degrees: number) => (degrees * Math.PI) / 180;
  const dLat = toRad(b[1] - a[1]);
  const dLng = toRad(b[0] - a[0]);
  const lat1 = toRad(a[1]);
  const lat2 = toRad(b[1]);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.min(1, Math.sqrt(h)));
};

/** Decode a Google encoded polyline into MapLibre [lng, lat] coordinates. */
const decodePolyline = (encoded: string): LngLat[] => {
  const points: LngLat[] = [];
  let index = 0;
  let latitude = 0;
  let longitude = 0;
  while (index < encoded.length) {
    let result = 0;
    let shift = 0;
    let byte = 0;
    do {
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    latitude += result & 1 ? ~(result >> 1) : result >> 1;
    result = 0;
    shift = 0;
    do {
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    longitude += result & 1 ? ~(result >> 1) : result >> 1;
    points.push([longitude / 1e5, latitude / 1e5]);
  }
  return points;
};

const boundsFromCoordinates = (coordinates: LngLat[]) => {
  let minLng = Infinity;
  let minLat = Infinity;
  let maxLng = -Infinity;
  let maxLat = -Infinity;
  for (const [lng, lat] of coordinates) {
    minLng = Math.min(minLng, lng);
    minLat = Math.min(minLat, lat);
    maxLng = Math.max(maxLng, lng);
    maxLat = Math.max(maxLat, lat);
  }
  return [[minLng, minLat], [maxLng, maxLat]] as [[number, number], [number, number]];
};

export default function QuestMap({
  quests,
  activeQuest,
  onSelectQuest,
  homeCenter,
  homeLabel = "Your home zone",
  level,
  completedCount,
  className,
  dateLabel,
  xp,
  refreshAvailable,
  onRefresh,
  onGenerate,
  generating,
}: QuestMapProps) {
  const center = useMemo<LngLat | null>(
    () =>
      homeCenter && Number.isFinite(homeCenter.latitude) && Number.isFinite(homeCenter.longitude)
        ? [homeCenter.longitude, homeCenter.latitude]
        : null,
    [homeCenter],
  );
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const markersRef = useRef<Marker[]>([]);
  /** Fixed saved home pin — never moves with GPS. */
  const homeMarkerRef = useRef<Marker | null>(null);
  /** Live “you are here” pin — updates as current location varies. */
  const playerMarkerRef = useRef<Marker | null>(null);
  const locationWatchRef = useRef<number | null>(null);
  const routeRequestRef = useRef(0);
  const fullScreenRef = useRef(false);
  const playerLocationRef = useRef<LivePosition | null>(null);
  const lastRouteOriginRef = useRef<LngLat | null>(null);
  const lastRerouteAtRef = useRef(0);
  const locationDeniedRef = useRef(false);

  const [mapReady, setMapReady] = useState(false);
  const [mapFailed, setMapFailed] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [trayExpanded, setTrayExpanded] = useState(false);
  const [routeStep, setRouteStep] = useState<"idle" | "loading" | "error">("idle");
  const [routeSummary, setRouteSummary] = useState<RouteSummary | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  /** Ephemeral device GPS — varies as the player moves; never written to the profile. */
  const [playerLocation, setPlayerLocation] = useState<LivePosition | null>(null);
  const [edgeIndicators, setEdgeIndicators] = useState<EdgeIndicator[]>([]);
  const [clock, setClock] = useState(() => Date.now());

  fullScreenRef.current = fullScreen;
  playerLocationRef.current = playerLocation;

  const questPoints = useMemo(
    () =>
      quests.flatMap((quest) =>
        Number.isFinite(quest.longitude) && Number.isFinite(quest.latitude)
          ? [{ quest, coordinates: [quest.longitude!, quest.latitude!] as LngLat }]
          : [],
      ),
    [quests],
  );
  const anchor = useMemo<LngLat | null>(
    () => (playerLocation ? [playerLocation.longitude, playerLocation.latitude] : center),
    [center, playerLocation],
  );
  const anchorRef = useRef<LngLat | null>(anchor);
  anchorRef.current = anchor;

  const activeQuestId = activeQuest?.id ?? null;
  const activeQuestStatus = activeQuest?.status ?? null;
  const activePoint = useMemo(
    () => (activeQuestId ? questPoints.find(({ quest }) => quest.id === activeQuestId) ?? null : null),
    [activeQuestId, questPoints],
  );
  const activePointRef = useRef(activePoint);
  activePointRef.current = activePoint;
  const activePointKey = activePoint ? activePoint.coordinates.join(",") : null;
  const centerRef = useRef(center);
  centerRef.current = center;

  const clearRoute = useCallback(() => {
    routeRequestRef.current += 1;
    const map = mapRef.current;
    if (map?.getLayer(ROUTE_INNER_LAYER)) map.removeLayer(ROUTE_INNER_LAYER);
    if (map?.getLayer(ROUTE_CASING_LAYER)) map.removeLayer(ROUTE_CASING_LAYER);
    if (map?.getSource(ROUTE_SOURCE)) map.removeSource(ROUTE_SOURCE);
    setRouteSummary(null);
    setRouteStep("idle");
    lastRouteOriginRef.current = null;
  }, []);

  const stopLocationTracking = useCallback(() => {
    if (locationWatchRef.current != null) navigator.geolocation?.clearWatch(locationWatchRef.current);
    locationWatchRef.current = null;
  }, []);

  const applyLivePosition = useCallback((coords: GeolocationCoordinates) => {
    const next: LivePosition = {
      longitude: coords.longitude,
      latitude: coords.latitude,
      heading: Number.isFinite(coords.heading) ? coords.heading : null,
    };
    playerLocationRef.current = next;
    setPlayerLocation(next);
    return next;
  }, []);

  /**
   * Continuously watch device GPS so current location can vary over time.
   * Home stays fixed on the server/profile; this never updates home.
   */
  const startLiveTracking = useCallback(
    (options?: { flyToFirst?: boolean; highAccuracy?: boolean }) => {
      if (!navigator.geolocation) return;
      if (locationDeniedRef.current) return;
      stopLocationTracking();
      let firstFix = true;
      locationWatchRef.current = navigator.geolocation.watchPosition(
        ({ coords }) => {
          applyLivePosition(coords);
          if (firstFix && options?.flyToFirst) {
            firstFix = false;
            mapRef.current?.flyTo({
              center: [coords.longitude, coords.latitude],
              ...GAME_CAMERA,
              offset: [0, 72],
              essential: true,
            });
          }
        },
        () => {
          locationDeniedRef.current = true;
          stopLocationTracking();
          setMessage("Location was not shared. Map falls back to your saved home zone; routes use home until GPS is allowed.");
        },
        {
          enableHighAccuracy: options?.highAccuracy ?? true,
          timeout: 12_000,
          maximumAge: 15_000,
        },
      );
    },
    [applyLivePosition, stopLocationTracking],
  );

  const focusAnchor = useCallback(() => {
    const map = mapRef.current;
    const currentAnchor = anchorRef.current;
    if (!map || !currentAnchor) return;
    map.flyTo({
      center: currentAnchor,
      ...GAME_CAMERA,
      offset: [0, Math.min(110, map.getContainer().clientHeight * 0.18)],
      duration: 550,
      essential: true,
    });
  }, []);

  const addRoute = useCallback((coordinates: LngLat[], options?: { fit?: boolean }) => {
    const map = mapRef.current;
    if (!map || coordinates.length < 2) return;
    if (map.getLayer(ROUTE_INNER_LAYER)) map.removeLayer(ROUTE_INNER_LAYER);
    if (map.getLayer(ROUTE_CASING_LAYER)) map.removeLayer(ROUTE_CASING_LAYER);
    if (map.getSource(ROUTE_SOURCE)) map.removeSource(ROUTE_SOURCE);
    map.addSource(ROUTE_SOURCE, {
      type: "geojson",
      data: {
        type: "Feature",
        properties: {},
        geometry: { type: "LineString", coordinates },
      },
    });
    map.addLayer({
      id: ROUTE_CASING_LAYER,
      type: "line",
      source: ROUTE_SOURCE,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#fff3db", "line-width": 9, "line-opacity": 0.95 },
    });
    map.addLayer({
      id: ROUTE_INNER_LAYER,
      type: "line",
      source: ROUTE_SOURCE,
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#ff745d",
        "line-width": 3.5,
        "line-opacity": 0.95,
        "line-dasharray": [1.5, 1.5],
      },
    });
    if (options?.fit === false) return;
    const padding = fullScreenRef.current
      ? { top: 90, bottom: 140, left: 40, right: 40 }
      : { top: 90, bottom: 130, left: 36, right: 36 };
    map.fitBounds(boundsFromCoordinates(coordinates), {
      padding,
      pitch: GAME_CAMERA.pitch,
      bearing: GAME_CAMERA.bearing,
      duration: 700,
      essential: true,
      maxZoom: 17,
    });
  }, []);

  /**
   * Route origin prefers the latest live fix (which can change as the player moves).
   * Home is only a fallback when GPS is unavailable — not a substitute for “here”.
   */
  const resolveOrigin = useCallback((): Promise<{ origin: LngLat; live: boolean; note?: string }> => {
    const live = playerLocationRef.current;
    if (live) {
      return Promise.resolve({
        origin: [live.longitude, live.latitude],
        live: true,
      });
    }
    const home = centerRef.current;
    if (!navigator.geolocation) {
      if (!home) return Promise.reject(new Error("no-origin"));
      return Promise.resolve({
        origin: home,
        live: false,
        note: "Live location is not available. Routing from your saved home zone.",
      });
    }
    return new Promise((resolve, reject) => {
      navigator.geolocation.getCurrentPosition(
        ({ coords }) => {
          applyLivePosition(coords);
          resolve({
            origin: [coords.longitude, coords.latitude],
            live: true,
          });
        },
        () => {
          locationDeniedRef.current = true;
          if (!home) {
            reject(new Error("location-denied"));
            return;
          }
          resolve({
            origin: home,
            live: false,
            note: "Location was not shared. Routing from your saved home zone.",
          });
        },
        { enableHighAccuracy: true, timeout: 10_000, maximumAge: 15_000 },
      );
    });
  }, [applyLivePosition]);

  const loadRoute = useCallback(
    async (destination: LngLat, options?: { fit?: boolean }) => {
      const requestId = ++routeRequestRef.current;
      setRouteStep("loading");
      try {
        const { origin, note } = await resolveOrigin();
        if (requestId !== routeRequestRef.current) return;
        if (note) setMessage(note);
        const route = await questApi.routePreview(
          { latitude: origin[1], longitude: origin[0] },
          { latitude: destination[1], longitude: destination[0] },
          "walking",
        );
        if (requestId !== routeRequestRef.current) return;
        const coordinates = route.encodedPolyline
          ? decodePolyline(route.encodedPolyline)
          : [origin, destination];
        if (coordinates.length < 2) throw new Error("empty-route");
        addRoute(coordinates, { fit: options?.fit !== false });
        lastRouteOriginRef.current = origin;
        lastRerouteAtRef.current = Date.now();
        setRouteSummary({
          distanceMeters: route.distanceMeters,
          durationSeconds: route.durationSeconds,
        });
        setRouteStep("idle");
      } catch (error) {
        if (requestId !== routeRequestRef.current) return;
        const reason = error instanceof Error ? error.message : "";
        if (reason === "location-denied" || reason === "no-origin") {
          setMessage("Allow location access (or set a home zone) to show the route to this quest.");
        } else {
          setMessage("Could not load a Google walking route. The destination is still on the map.");
        }
        setRouteStep("error");
      }
    },
    [addRoute, resolveOrigin],
  );

  useEffect(() => {
    if (!mapContainer.current || mapRef.current || !center) return;
    let cancelled = false;
    let map: MapLibreMap | null = null;
    let loadTimeout: number | null = null;
    const failMap = () => {
      if (!cancelled) setMapFailed(true);
    };
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      if (cancelled || !mapContainer.current) return;
      map = new maplibregl.Map({ container: mapContainer.current, style: gameMapStyle, center, ...GAME_CAMERA });
      mapRef.current = map;
      map.on("load", () => {
        if (loadTimeout != null) window.clearTimeout(loadTimeout);
        if (!cancelled) {
          setMapReady(true);
          window.requestAnimationFrame(focusAnchor);
        }
      });
      map.on("error", (event) => {
        if (/style|stylesheet|parse|unrecoverable/i.test(event.error?.message ?? "")) failMap();
      });
      loadTimeout = window.setTimeout(() => {
        if (!map?.loaded()) failMap();
      }, MAP_LOAD_TIMEOUT_MS);
    }).catch(failMap);
    return () => {
      cancelled = true;
      if (loadTimeout != null) window.clearTimeout(loadTimeout);
      markersRef.current.forEach((marker) => marker.remove());
      homeMarkerRef.current?.remove();
      playerMarkerRef.current?.remove();
      stopLocationTracking();
      map?.remove();
      mapRef.current = null;
    };
  }, [center, focusAnchor, stopLocationTracking]);

  // Start continuous GPS once the map is ready so current location can vary while playing.
  useEffect(() => {
    if (!mapReady || mapFailed) return;
    startLiveTracking({ flyToFirst: false, highAccuracy: true });
    return () => stopLocationTracking();
  }, [mapFailed, mapReady, startLiveTracking, stopLocationTracking]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) return;
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = questPoints.map(({ quest, coordinates }) => {
        const marker = document.createElement("button");
        marker.type = "button";
        marker.className = `${styles.questMarker} ${styles[`state${quest.status[0].toUpperCase()}${quest.status.slice(1)}`]} ${quest.id === activeQuest?.id ? styles.selectedMarker : ""}`;
        marker.setAttribute("aria-label", `Open quest: ${quest.title}`);
        marker.style.setProperty("--beacon-accent", ACCENT_COLORS[quest.accent] ?? ACCENT_COLORS.coral);
        marker.innerHTML = `<span class="${styles.beaconGround}" aria-hidden="true"></span><span class="${styles.beaconStem}" aria-hidden="true"></span><span class="${styles.beaconCap}"><span class="${styles.beaconIcon}">${stateIcon(quest.status, quest.emoji)}</span></span>`;
        marker.addEventListener("click", () => onSelectQuest(quest));
        return new maplibregl.Marker({
          element: marker,
          anchor: "bottom",
          offset: [0, -10],
          rotationAlignment: "viewport",
          pitchAlignment: "viewport",
        })
          .setLngLat(coordinates)
          .addTo(map);
      });
    });
  }, [activeQuest?.id, mapReady, onSelectQuest, questPoints]);

  // Fixed home pin (saved profile base) — independent of varying GPS.
  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map || !center) return;
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      homeMarkerRef.current?.remove();
      const marker = document.createElement("div");
      marker.className = `${styles.playerMarker} ${styles.homePlayer}`;
      marker.setAttribute("aria-label", `${homeLabel}, saved home position`);
      marker.setAttribute("role", "img");
      marker.innerHTML = `<span class="${styles.playerHalo}" aria-hidden="true"></span><span class="${styles.playerDisc}"><span class="${styles.playerArrow}" aria-hidden="true"></span></span><span class="${styles.homeLabel}">Home</span>`;
      homeMarkerRef.current = new maplibregl.Marker({
        element: marker,
        anchor: "center",
        rotationAlignment: "viewport",
        pitchAlignment: "viewport",
      })
        .setLngLat(center)
        .addTo(map);
    });
  }, [center, homeLabel, mapReady]);

  // Live player pin — moves whenever current location updates.
  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map || !playerLocation) {
      playerMarkerRef.current?.remove();
      playerMarkerRef.current = null;
      return;
    }
    const position: LngLat = [playerLocation.longitude, playerLocation.latitude];
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      if (playerMarkerRef.current) {
        playerMarkerRef.current.setLngLat(position);
        const arrow = playerMarkerRef.current.getElement().querySelector(`.${styles.playerArrow}`) as HTMLElement | null;
        if (arrow) arrow.style.transform = `rotate(${playerLocation.heading ?? 0}deg)`;
        return;
      }
      const marker = document.createElement("div");
      marker.className = `${styles.playerMarker} ${styles.livePlayer}`;
      marker.setAttribute("aria-label", "Your current location");
      marker.setAttribute("role", "img");
      marker.innerHTML = `<span class="${styles.playerHalo}" aria-hidden="true"></span><span class="${styles.playerDisc}"><span class="${styles.playerArrow}" aria-hidden="true"></span></span><span class="${styles.homeLabel}">You</span>`;
      const arrow = marker.querySelector(`.${styles.playerArrow}`) as HTMLElement | null;
      if (arrow) arrow.style.transform = `rotate(${playerLocation.heading ?? 0}deg)`;
      playerMarkerRef.current = new maplibregl.Marker({
        element: marker,
        anchor: "center",
        rotationAlignment: "viewport",
        pitchAlignment: "viewport",
      })
        .setLngLat(position)
        .addTo(map);
    });
  }, [mapReady, playerLocation]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) return;
    const updateIndicators = () => {
      const rect = map.getContainer().getBoundingClientRect();
      const top = 70;
      const bottom = fullScreen ? 24 : trayExpanded ? 250 : 100;
      const left = 18;
      const right = 18;
      const safeWidth = rect.width - left - right;
      const safeHeight = rect.height - top - bottom;
      if (safeWidth <= 0 || safeHeight <= 0) return;
      const occupied = new Map<string, number>();
      const next = questPoints.flatMap(({ quest, coordinates }) => {
        const point = map.project(coordinates);
        const visible =
          point.x >= left &&
          point.x <= rect.width - right &&
          point.y >= top &&
          point.y <= rect.height - bottom;
        if (visible) return [];
        const dx = point.x - rect.width / 2;
        const dy = point.y - rect.height / 2;
        const scale = Math.min(
          1,
          Math.abs(dx) > (Math.abs(dy) * safeWidth) / safeHeight
            ? safeWidth / 2 / Math.abs(dx || 1)
            : safeHeight / 2 / Math.abs(dy || 1),
        );
        let x = rect.width / 2 + dx * scale;
        let y = rect.height / 2 + dy * scale;
        const edge =
          x <= left + 1
            ? "left"
            : x >= rect.width - right - 1
              ? "right"
              : y <= top + 1
                ? "top"
                : "bottom";
        const slot = occupied.get(edge) ?? 0;
        occupied.set(edge, slot + 1);
        if (edge === "left" || edge === "right") {
          y = Math.max(top + 18, Math.min(rect.height - bottom - 18, y + (slot - 1) * 36));
        } else {
          x = Math.max(left + 24, Math.min(rect.width - right - 24, x + (slot - 1) * 64));
        }
        const from = anchor ?? center;
        const distance = from
          ? Math.hypot(
              (coordinates[0] - from[0]) * 111_320 * Math.cos((from[1] * Math.PI) / 180),
              (coordinates[1] - from[1]) * 110_540,
            )
          : 0;
        return [{ quest, x, y, angle: (Math.atan2(dy, dx) * 180) / Math.PI + 90, distance }];
      });
      setEdgeIndicators(next);
    };
    updateIndicators();
    map.on("move", updateIndicators);
    map.on("zoom", updateIndicators);
    map.on("resize", updateIndicators);
    return () => {
      map.off("move", updateIndicators);
      map.off("zoom", updateIndicators);
      map.off("resize", updateIndicators);
    };
  }, [anchor, center, fullScreen, mapReady, questPoints, trayExpanded]);

  // When a quest is selected (sheet / tray), focus the destination briefly.
  useEffect(() => {
    const map = mapRef.current;
    const currentActivePoint = activePointRef.current;
    if (!map || !currentActivePoint || activeQuestStatus === "active") return;
    setTrayExpanded(false);
    map.flyTo({
      center: currentActivePoint.coordinates,
      zoom: 16,
      pitch: GAME_CAMERA.pitch,
      bearing: GAME_CAMERA.bearing,
      duration: 600,
      essential: true,
    });
  }, [activePointKey, activeQuestId, activeQuestStatus, mapReady]);

  // Auto-start Google walking route when a quest becomes active (from current location if known).
  useEffect(() => {
    const map = mapRef.current;
    const currentActivePoint = activePointRef.current;
    if (!mapReady || !map) return;
    if (!activeQuestId || activeQuestStatus !== "active" || !currentActivePoint) {
      clearRoute();
      return;
    }
    setTrayExpanded(false);
    void loadRoute(currentActivePoint.coordinates, { fit: true });
    return () => {
      routeRequestRef.current += 1;
    };
  }, [activePointKey, activeQuestId, activeQuestStatus, clearRoute, loadRoute, mapReady]);

  // Current location varies — re-route when the player has moved far enough from the last origin.
  useEffect(() => {
    if (activeQuestStatus !== "active" || !playerLocation || !activePoint) return;
    if (routeStep === "loading") return;
    const current: LngLat = [playerLocation.longitude, playerLocation.latitude];
    const lastOrigin = lastRouteOriginRef.current;
    if (!lastOrigin) return;
    if (distanceMeters(lastOrigin, current) < REROUTE_DISTANCE_METERS) return;
    if (Date.now() - lastRerouteAtRef.current < REROUTE_COOLDOWN_MS) return;
    const destination = activePoint.coordinates;
    const timer = window.setTimeout(() => {
      void loadRoute(destination, { fit: false });
    }, 1_200);
    return () => window.clearTimeout(timer);
  }, [activePoint, activeQuestStatus, loadRoute, playerLocation, routeStep]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    window.setTimeout(() => map.resize(), 0);
  }, [fullScreen, trayExpanded]);

  const locatePlayer = () => {
    if (!navigator.geolocation) {
      setMessage("Live location is not available in this browser.");
      return;
    }
    locationDeniedRef.current = false;
    startLiveTracking({ flyToFirst: true, highAccuracy: true });
  };

  useEffect(() => {
    if (!activeQuest?.startExpiresAt) return;
    const interval = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [activeQuest?.startExpiresAt]);

  const requestRoute = () => {
    const destination = activePoint?.coordinates;
    if (destination) void loadRoute(destination);
  };

  const routeLabel =
    routeStep === "loading"
      ? "Finding Google route…"
      : routeSummary
        ? "Refresh route"
        : "Show route";
  const routeSummaryText = routeSummary
    ? `${metersText(routeSummary.distanceMeters)} · ${minutesText(routeSummary.durationSeconds)}`
    : routeStep === "loading"
      ? "Finding route…"
      : null;
  const activeRemaining = activeQuest?.startExpiresAt
    ? new Date(activeQuest.startExpiresAt).getTime() - clock
    : null;
  const showActiveRouteUi = activeQuestStatus === "active" && !!activePoint;

  return (
    <section
      className={`${styles.shell} ${fullScreen ? styles.expanded : ""} ${className ?? ""}`}
      aria-label="Quest map"
    >
      <div className={styles.mapSurface}>
        {!mapFailed && <div ref={mapContainer} className={styles.mapCanvas} />}
        {(mapFailed || !center) && (
          <div className={styles.emptyMap} role="status">
            {center ? "Map tiles are unavailable." : "Set a home location to view the map."}
          </div>
        )}
      </div>

      <div className={styles.hud}>
        <div className={styles.statusLine}>
          <span>{dateLabel}</span>
          {level != null && <span>LVL {level}</span>}
          {xp != null && <span>{xp} XP</span>}
          {completedCount != null && (
            <span>
              {completedCount}/{quests.length} done
            </span>
          )}
          {activeRemaining != null && (
            <span className={styles.timerBadge}>⏱ {timerText(activeRemaining)}</span>
          )}
        </div>
        <div className={styles.mapControls}>
          {!mapFailed && (
            <>
              <button type="button" onClick={locatePlayer} aria-label="Find my live location">
                ⌖
              </button>
              <button type="button" onClick={focusAnchor} aria-label="Recenter on you or saved home">
                ⌂
              </button>
            </>
          )}
          <button
            type="button"
            onClick={() => setFullScreen((value) => !value)}
            aria-label={fullScreen ? "Close full-screen map" : "Expand map"}
          >
            {fullScreen ? "×" : "⛶"}
          </button>
        </div>
      </div>

      {edgeIndicators.map(({ quest, x, y, angle, distance }) => (
        <button
          key={quest.id}
          type="button"
          className={`${styles.edgeIndicator} ${styles[`edge${quest.status[0].toUpperCase()}${quest.status.slice(1)}`] ?? ""}`}
          onClick={() => onSelectQuest(quest)}
          aria-label={`Open ${quest.title}, ${metersText(distance)} away`}
          style={{ left: x, top: y }}
        >
          <i style={{ transform: `rotate(${angle}deg)` }}>▲</i>
          <b>{stateIcon(quest.status, quest.emoji)}</b>
          <small>{metersText(distance)}</small>
        </button>
      ))}

      {/* Compact map (minimap): route chip above the tray */}
      {!fullScreen && showActiveRouteUi && routeSummaryText && (
        <div className={styles.routeChip} role="status">
          <span>↗</span>
          <b>{routeSummaryText}</b>
          {routeStep === "error" && (
            <button type="button" onClick={requestRoute}>
              Retry
            </button>
          )}
        </div>
      )}

      {!fullScreen && (
        <section
          className={`${styles.questTray} ${trayExpanded ? styles.trayExpanded : ""}`}
          aria-label="Today's drops"
        >
          <button
            type="button"
            className={styles.trayHandle}
            onClick={() => setTrayExpanded((value) => !value)}
            aria-expanded={trayExpanded}
          >
            <i />
            <span>Today’s drops</span>
            <b>
              {quests.length === 0
                ? "✦ Generate quests"
                : `${quests.find((quest) => quest.status === "offered")?.emoji ?? "✓"} ${quests.find((quest) => quest.status === "offered")?.title ?? "All done"}`}
            </b>
            <em>{trayExpanded ? "⌄" : "⌃"}</em>
          </button>
          {trayExpanded && (
            <>
              <div className={styles.trayHeading}>
                {quests.length === 0 ? (
                  <button onClick={onGenerate} disabled={generating}>
                    {generating ? "Generating quests…" : "✦ Generate quests"}
                  </button>
                ) : refreshAvailable ? (
                  <button onClick={onRefresh}>↻ Refresh deck</button>
                ) : (
                  <span>Deck locked</span>
                )}
              </div>
              <div className={styles.questScroll}>
                {quests.map((quest) => (
                  <button
                    className={`${styles.miniQuest} ${styles[quest.status] ?? ""}`}
                    onClick={() => onSelectQuest(quest)}
                    key={quest.id}
                  >
                    <span className={`${styles.miniIcon} ${styles[quest.accent] ?? ""}`}>
                      {stateIcon(quest.status, quest.emoji)}
                    </span>
                    <small>{quest.category}</small>
                    <b>{quest.title}</b>
                    <em>+{quest.xp} XP</em>
                  </button>
                ))}
              </div>
            </>
          )}
        </section>
      )}

      {/* Full-screen map (main map): destination + route summary / refresh */}
      {fullScreen && (
        <div className={styles.expandedPanel}>
          <p>{activeQuest ? activeQuest.place || activeQuest.title : homeLabel}</p>
          {showActiveRouteUi && !mapFailed && (
            <>
              <button
                type="button"
                className={styles.routeButton}
                onClick={requestRoute}
                disabled={routeStep === "loading"}
              >
                {routeLabel}
              </button>
              {routeStep === "loading" && (
                <small>Using your location to build a Google walking route.</small>
              )}
              {routeSummary && (
                <div className={styles.routeSummary}>
                  {routeSummaryText}
                  <button type="button" onClick={clearRoute}>
                    Clear
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {message && (
        <div role="status" className={styles.message}>
          {message}
          <button type="button" onClick={() => setMessage(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
    </section>
  );
}
