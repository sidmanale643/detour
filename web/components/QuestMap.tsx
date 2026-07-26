"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Map as MapLibreMap, Marker } from "maplibre-gl";
import type { Feature, LineString } from "geojson";
import { questApi, type Coordinate, type Quest, type TravelMode } from "../lib/quest-api";
import { gameMapStyle } from "../lib/osm-map";
import styles from "./QuestMap.module.css";

export type QuestMapStatus = "offered" | "active" | "completed" | "skipped" | "superseded" | "expired";
export interface QuestMapQuest { id: string; title: string; emoji: string; status: QuestMapStatus; accent?: string; place?: string; coordinates?: readonly [longitude: number, latitude: number]; }
export interface QuestMapProps {
  quests: readonly Quest[]; activeQuest: Quest | null; onSelectQuest: (quest: Quest) => void;
  homeCenter: Coordinate | null; homeLabel?: string; level?: number; completedCount?: number; className?: string;
  dateLabel?: string; xp?: number; refreshAvailable?: boolean; onRefresh?: () => void;
  onGenerate?: () => void; generating?: boolean; travelModes?: readonly TravelMode[];
}
type RouteSummary = { distanceMeters: number; durationSeconds: number };
type EdgeIndicator = { quest: Quest; x: number; y: number; angle: number; distance: number };
const ROUTE_SOURCE = "detour-route";
const ROUTE_CASING_LAYER = "detour-route-casing";
const ROUTE_INNER_LAYER = "detour-route-line";
const GAME_CAMERA = { zoom: 16, pitch: 50, bearing: -18 } as const;
const MAP_LOAD_TIMEOUT_MS = 15_000;
const ACCENT_COLORS: Record<string, string> = { coral: "#ff745d", aqua: "#43d3d4", purple: "#b697f4", gold: "#ffc954", mint: "#83d6a6", blue: "#78a8f6" };
const stateIcon = (status: QuestMapStatus, emoji: string) => status === "completed" ? "✓" : status === "skipped" || status === "expired" ? "–" : emoji;
const metersText = (meters: number) => meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`;
const modeLabel = (mode: TravelMode) => ({
  walking: "walk",
  cycling: "cycle",
  two_wheeler: "ride",
  four_wheeler: "drive",
  public_transport: "public transport",
}[mode]);
const minutesText = (seconds: number, mode: TravelMode) => `${Math.max(1, Math.round(seconds / 60))} min ${modeLabel(mode)}`;
const timerText = (milliseconds: number) => { const total = Math.max(0, Math.ceil(milliseconds / 1000)); return `${Math.floor(total / 3600)}:${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`; };
const decodePolyline = (encoded: string): [number, number][] => {
  const points: [number, number][] = [];
  let index = 0, latitude = 0, longitude = 0;
  while (index < encoded.length) {
    let result = 0, shift = 0, byte = 0;
    do { byte = encoded.charCodeAt(index++) - 63; result |= (byte & 0x1f) << shift; shift += 5; } while (byte >= 0x20);
    latitude += result & 1 ? ~(result >> 1) : result >> 1;
    result = 0; shift = 0;
    do { byte = encoded.charCodeAt(index++) - 63; result |= (byte & 0x1f) << shift; shift += 5; } while (byte >= 0x20);
    longitude += result & 1 ? ~(result >> 1) : result >> 1;
    points.push([longitude / 1e5, latitude / 1e5]);
  }
  return points;
};

export default function QuestMap({ quests, activeQuest, onSelectQuest, homeCenter, homeLabel = "Your home zone", level, completedCount, className, dateLabel, xp, refreshAvailable, onRefresh, onGenerate, generating, travelModes = ["walking"] }: QuestMapProps) {
  const center = useMemo<[number, number] | null>(() => homeCenter && Number.isFinite(homeCenter.latitude) && Number.isFinite(homeCenter.longitude) ? [homeCenter.longitude, homeCenter.latitude] : null, [homeCenter]);
  const mapContainer = useRef<HTMLDivElement>(null); const mapRef = useRef<MapLibreMap | null>(null);
  const markersRef = useRef<Marker[]>([]); const playerMarkerRef = useRef<Marker | null>(null); const locationWatchRef = useRef<number | null>(null); const routeRequestRef = useRef(0);
  const [mapReady, setMapReady] = useState(false); const [mapFailed, setMapFailed] = useState(false); const [fullScreen, setFullScreen] = useState(false); const [trayExpanded, setTrayExpanded] = useState(false);
  const [routeStep, setRouteStep] = useState<"idle" | "consent" | "loading">("idle"); const [routeSummary, setRouteSummary] = useState<RouteSummary | null>(null); const [message, setMessage] = useState<string | null>(null);
  const [playerLocation, setPlayerLocation] = useState<{ longitude: number; latitude: number; heading: number | null } | null>(null); const [edgeIndicators, setEdgeIndicators] = useState<EdgeIndicator[]>([]); const [clock, setClock] = useState(() => Date.now());
  const questPoints = useMemo(() => quests.flatMap((quest) => Number.isFinite(quest.longitude) && Number.isFinite(quest.latitude) ? [{ quest, coordinates: [quest.longitude!, quest.latitude!] as [number, number] }] : []), [quests]);
  const anchor = useMemo<[number, number] | null>(() => playerLocation ? [playerLocation.longitude, playerLocation.latitude] : center, [center, playerLocation]);
  const anchorRef = useRef<[number, number] | null>(anchor);
  anchorRef.current = anchor;
  const activeQuestId = activeQuest?.id ?? null;
  const activeQuestStatus = activeQuest?.status ?? null;
  const activePoint = useMemo(() => activeQuestId ? questPoints.find(({ quest }) => quest.id === activeQuestId) ?? null : null, [activeQuestId, questPoints]);
  const activePointRef = useRef(activePoint);
  activePointRef.current = activePoint;
  const activePointKey = activePoint ? activePoint.coordinates.join(",") : null;
  const clearRoute = useCallback(() => { routeRequestRef.current += 1; const map = mapRef.current; if (map?.getLayer(ROUTE_INNER_LAYER)) map.removeLayer(ROUTE_INNER_LAYER); if (map?.getLayer(ROUTE_CASING_LAYER)) map.removeLayer(ROUTE_CASING_LAYER); if (map?.getSource(ROUTE_SOURCE)) map.removeSource(ROUTE_SOURCE); setRouteSummary(null); setRouteStep("idle"); }, []);
  const stopLocationTracking = useCallback(() => { if (locationWatchRef.current != null) navigator.geolocation?.clearWatch(locationWatchRef.current); locationWatchRef.current = null; }, []);
  const focusAnchor = useCallback(() => { const map = mapRef.current; const currentAnchor = anchorRef.current; if (!map || !currentAnchor) return; map.flyTo({ center: currentAnchor, ...GAME_CAMERA, offset: [0, Math.min(110, map.getContainer().clientHeight * .18)], duration: 550, essential: true }); }, []);
  const addRoute = useCallback((coordinates: unknown) => { const map = mapRef.current; if (!map) return; map.addSource(ROUTE_SOURCE, { type: "geojson", data: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates } } as GeoJSON.Feature<GeoJSON.LineString> }); map.addLayer({ id: ROUTE_CASING_LAYER, type: "line", source: ROUTE_SOURCE, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff3db", "line-width": 9, "line-opacity": .95 } }); map.addLayer({ id: ROUTE_INNER_LAYER, type: "line", source: ROUTE_SOURCE, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#ff745d", "line-width": 3.5, "line-opacity": .95, "line-dasharray": [1.5, 1.5] } }); }, []);
  const loadRoute = useCallback((destination: [number, number], mode: TravelMode) => {
    clearRoute();
    if (!navigator.geolocation) {
      setMessage("Live location is not available. The quest destination is still visible on the map.");
      setRouteStep("consent");
      return;
    }
    const requestId = routeRequestRef.current;
    setRouteStep("loading");
    navigator.geolocation.getCurrentPosition(async ({ coords }) => {
      if (requestId !== routeRequestRef.current) return;
      try {
        const route = await questApi.routePreview(
          { latitude: coords.latitude, longitude: coords.longitude },
          { latitude: destination[1], longitude: destination[0] },
          mode
        );
        if (requestId !== routeRequestRef.current) return;
        if (route.encodedPolyline) addRoute(decodePolyline(route.encodedPolyline));
        setRouteSummary({ distanceMeters: route.distanceMeters, durationSeconds: route.durationSeconds });
        setRouteStep("idle");
      } catch {
        if (requestId !== routeRequestRef.current) return;
        setMessage(`Could not find a ${modeLabel(mode)} GPS route. Please try again.`);
        setRouteStep("consent");
      }
    }, () => {
      if (requestId !== routeRequestRef.current) return;
      setMessage("Location was not shared. Allow GPS access to show the path to this quest.");
      setRouteStep("consent");
    }, { enableHighAccuracy: true, timeout: 10_000, maximumAge: 60_000 });
  }, [addRoute, clearRoute]);

  useEffect(() => {
    if (!mapContainer.current || mapRef.current || !center) return; let cancelled = false; let map: MapLibreMap | null = null; let loadTimeout: number | null = null;
    const failMap = () => { if (!cancelled) setMapFailed(true); };
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      if (cancelled || !mapContainer.current) return; map = new maplibregl.Map({ container: mapContainer.current, style: gameMapStyle, center, ...GAME_CAMERA }); mapRef.current = map;
      map.on("load", () => { if (loadTimeout != null) window.clearTimeout(loadTimeout); if (!cancelled) { setMapReady(true); window.requestAnimationFrame(focusAnchor); } });
      map.on("error", (event) => { if (/style|stylesheet|parse|unrecoverable/i.test(event.error?.message ?? "")) failMap(); }); loadTimeout = window.setTimeout(() => { if (!map?.loaded()) failMap(); }, MAP_LOAD_TIMEOUT_MS);
    }).catch(failMap);
    return () => { cancelled = true; if (loadTimeout != null) window.clearTimeout(loadTimeout); markersRef.current.forEach((marker) => marker.remove()); playerMarkerRef.current?.remove(); stopLocationTracking(); map?.remove(); mapRef.current = null; };
  }, [center, focusAnchor, stopLocationTracking]);

  useEffect(() => {
    const map = mapRef.current; if (!mapReady || !map) return;
    void import("maplibre-gl").then(({ default: maplibregl }) => { markersRef.current.forEach((marker) => marker.remove()); markersRef.current = questPoints.map(({ quest, coordinates }) => {
      const marker = document.createElement("button"); marker.type = "button"; marker.className = `${styles.questMarker} ${styles[`state${quest.status[0].toUpperCase()}${quest.status.slice(1)}`]} ${quest.id === activeQuest?.id ? styles.selectedMarker : ""}`;
      marker.setAttribute("aria-label", `Open quest: ${quest.title}`); marker.style.setProperty("--beacon-accent", ACCENT_COLORS[quest.accent] ?? ACCENT_COLORS.coral);
      marker.innerHTML = `<span class="${styles.beaconGround}" aria-hidden="true"></span><span class="${styles.beaconStem}" aria-hidden="true"></span><span class="${styles.beaconCap}"><span class="${styles.beaconIcon}">${stateIcon(quest.status, quest.emoji)}</span></span>`;
      marker.addEventListener("click", () => onSelectQuest(quest)); return new maplibregl.Marker({ element: marker, anchor: "bottom", offset: [0, -10], rotationAlignment: "viewport", pitchAlignment: "viewport" }).setLngLat(coordinates).addTo(map);
    }); });
  }, [activeQuest?.id, mapReady, onSelectQuest, questPoints]);

  useEffect(() => {
    const map = mapRef.current; if (!mapReady || !map || !center) return;
    void import("maplibre-gl").then(({ default: maplibregl }) => { playerMarkerRef.current?.remove(); const live = playerLocation !== null; const position: [number, number] = playerLocation ? [playerLocation.longitude, playerLocation.latitude] : center;
      const marker = document.createElement("div"); marker.className = `${styles.playerMarker} ${live ? styles.livePlayer : styles.homePlayer}`; marker.setAttribute("aria-label", live ? "Your live location" : `${homeLabel}, saved home position`); marker.setAttribute("role", "img");
      marker.innerHTML = `<span class="${styles.playerHalo}" aria-hidden="true"></span><span class="${styles.playerDisc}"><span class="${styles.playerArrow}" aria-hidden="true"></span></span>${live ? "" : `<span class="${styles.homeLabel}">Saved home</span>`}`;
      const arrow = marker.querySelector(`.${styles.playerArrow}`) as HTMLElement | null; if (arrow) arrow.style.transform = `rotate(${playerLocation?.heading ?? 0}deg)`;
      playerMarkerRef.current = new maplibregl.Marker({ element: marker, anchor: "center", rotationAlignment: "viewport", pitchAlignment: "viewport" }).setLngLat(position).addTo(map);
    });
  }, [center, homeLabel, mapReady, playerLocation]);

  useEffect(() => {
    const map = mapRef.current; if (!mapReady || !map) return;
    const updateIndicators = () => { const rect = map.getContainer().getBoundingClientRect(); const top = 70; const bottom = fullScreen ? 24 : trayExpanded ? 250 : 100; const left = 18; const right = 18; const safeWidth = rect.width - left - right; const safeHeight = rect.height - top - bottom;
      if (safeWidth <= 0 || safeHeight <= 0) return; const occupied = new Map<string, number>(); const next = questPoints.flatMap(({ quest, coordinates }) => { const point = map.project(coordinates); const visible = point.x >= left && point.x <= rect.width - right && point.y >= top && point.y <= rect.height - bottom; if (visible) return [];
        const dx = point.x - rect.width / 2, dy = point.y - rect.height / 2; const scale = Math.min(1, Math.abs(dx) > Math.abs(dy) * safeWidth / safeHeight ? (safeWidth / 2) / Math.abs(dx || 1) : (safeHeight / 2) / Math.abs(dy || 1)); let x = rect.width / 2 + dx * scale; let y = rect.height / 2 + dy * scale;
        const edge = x <= left + 1 ? "left" : x >= rect.width - right - 1 ? "right" : y <= top + 1 ? "top" : "bottom"; const slot = occupied.get(edge) ?? 0; occupied.set(edge, slot + 1); if (edge === "left" || edge === "right") y = Math.max(top + 18, Math.min(rect.height - bottom - 18, y + (slot - 1) * 36)); else x = Math.max(left + 24, Math.min(rect.width - right - 24, x + (slot - 1) * 64));
        const from = anchor ?? center; const distance = from ? Math.hypot((coordinates[0] - from[0]) * 111_320 * Math.cos(from[1] * Math.PI / 180), (coordinates[1] - from[1]) * 110_540) : 0; return [{ quest, x, y, angle: Math.atan2(dy, dx) * 180 / Math.PI + 90, distance }];
      }); setEdgeIndicators(next);
    }; updateIndicators(); map.on("move", updateIndicators); map.on("zoom", updateIndicators); map.on("resize", updateIndicators); return () => { map.off("move", updateIndicators); map.off("zoom", updateIndicators); map.off("resize", updateIndicators); };
  }, [anchor, center, fullScreen, mapReady, questPoints, trayExpanded]);

  useEffect(() => {
    const map = mapRef.current;
    const currentActivePoint = activePointRef.current;
    if (!map || !currentActivePoint) return;
    setTrayExpanded(false);
    map.flyTo({ center: currentActivePoint.coordinates, zoom: 16, pitch: GAME_CAMERA.pitch, bearing: GAME_CAMERA.bearing, duration: 600, essential: true });
  }, [activePointKey, activeQuestId, mapReady]);
  useEffect(() => {
    const map = mapRef.current;
    const currentActivePoint = activePointRef.current;
    if (!map || !activeQuestId || !currentActivePoint) {
      clearRoute();
      return;
    }
    clearRoute();
    const mode = activeQuest?.travelMode ?? travelModes[0] ?? null;
    if (activeQuestStatus === "active" && mode) loadRoute(currentActivePoint.coordinates, mode);
  }, [activePointKey, activeQuest?.travelMode, activeQuestId, activeQuestStatus, clearRoute, loadRoute, mapReady, travelModes]);
  useEffect(() => { const map = mapRef.current; if (!map) return; window.setTimeout(() => map.resize(), 0); }, [fullScreen, trayExpanded]);
  const locatePlayer = () => { if (!navigator.geolocation) { setMessage("Live location is not available in this browser."); return; } stopLocationTracking(); const update = ({ coords }: GeolocationPosition) => { const position = { longitude: coords.longitude, latitude: coords.latitude, heading: Number.isFinite(coords.heading) ? coords.heading : null }; setPlayerLocation(position); mapRef.current?.flyTo({ center: [position.longitude, position.latitude], ...GAME_CAMERA, offset: [0, 72], essential: true }); }; locationWatchRef.current = navigator.geolocation.watchPosition(update, () => { stopLocationTracking(); setMessage("Location was not shared. Showing your saved home zone instead."); }, { enableHighAccuracy: false, timeout: 10_000, maximumAge: 60_000 }); };
  useEffect(() => { if (!activeQuest?.startExpiresAt) return; const interval = window.setInterval(() => setClock(Date.now()), 1000); return () => window.clearInterval(interval); }, [activeQuest?.startExpiresAt]);
  const selectedRouteMode = activeQuest?.travelMode ?? travelModes[0] ?? null;
  const requestRoute = () => { const destination = activeQuest ? questPoints.find(({ quest }) => quest.id === activeQuest.id)?.coordinates : undefined; if (destination && selectedRouteMode) loadRoute(destination, selectedRouteMode); };
  const routeLabel = routeStep === "consent" ? "Allow location & show route" : routeStep === "loading" ? "Finding GPS route…" : routeSummary ? "Refresh GPS route" : "Show GPS route";
  const routeModeCopy = selectedRouteMode ? `your ${modeLabel(selectedRouteMode)} route` : "your route";
  const activeRemaining = activeQuest?.startExpiresAt ? new Date(activeQuest.startExpiresAt).getTime() - clock : null;
  return <section className={`${styles.shell} ${fullScreen ? styles.expanded : ""} ${className ?? ""}`} aria-label="Quest map"><div className={styles.mapSurface}>{!mapFailed && <div ref={mapContainer} className={styles.mapCanvas} />}{(mapFailed || !center) && <div className={styles.emptyMap} role="status">{center ? "Map tiles are unavailable." : "Set a home location to view the map."}</div>}</div>
    <div className={styles.hud}><div className={styles.statusLine}><span>{dateLabel}</span>{level != null && <span>LVL {level}</span>}{xp != null && <span>{xp} XP</span>}{completedCount != null && <span>{completedCount}/{quests.length} done</span>}{activeRemaining != null && <span className={styles.timerBadge}>⏱ {timerText(activeRemaining)}</span>}</div><div className={styles.mapControls}>{!mapFailed && <><button type="button" onClick={locatePlayer} aria-label="Find my live location">⌖</button><button type="button" onClick={focusAnchor} aria-label="Recenter on saved home">⌂</button></>}<button type="button" onClick={() => setFullScreen((value) => !value)} aria-label={fullScreen ? "Close full-screen map" : "Expand map"}>{fullScreen ? "×" : "⛶"}</button></div></div>
    {edgeIndicators.map(({ quest, x, y, angle, distance }) => <button key={quest.id} type="button" className={`${styles.edgeIndicator} ${styles[`edge${quest.status[0].toUpperCase()}${quest.status.slice(1)}`] ?? ""}`} onClick={() => onSelectQuest(quest)} aria-label={`Open ${quest.title}, ${metersText(distance)} away`} style={{ left: x, top: y }}><i style={{ transform: `rotate(${angle}deg)` }}>▲</i><b>{stateIcon(quest.status, quest.emoji)}</b><small>{metersText(distance)}</small></button>)}
    {!fullScreen && <section className={`${styles.questTray} ${trayExpanded ? styles.trayExpanded : ""}`} aria-label="Today's drops"><button type="button" className={styles.trayHandle} onClick={() => setTrayExpanded((value) => !value)} aria-expanded={trayExpanded}><i /><span>Today’s drops</span><b>{quests.length === 0 ? "✦ Generate quests" : `${quests.find((quest) => quest.status === "offered")?.emoji ?? "✓"} ${quests.find((quest) => quest.status === "offered")?.title ?? "All done"}`}</b><em>{trayExpanded ? "⌄" : "⌃"}</em></button>{trayExpanded && <><div className={styles.trayHeading}>{quests.length === 0 ? <button onClick={onGenerate} disabled={generating}>{generating ? "Generating quests…" : "✦ Generate quests"}</button> : refreshAvailable ? <button onClick={onRefresh}>↻ Refresh deck</button> : <span>Deck locked</span>}</div><div className={styles.questScroll}>{quests.map((quest) => <button className={`${styles.miniQuest} ${styles[quest.status] ?? ""}`} onClick={() => onSelectQuest(quest)} key={quest.id}><span className={`${styles.miniIcon} ${styles[quest.accent] ?? ""}`}>{stateIcon(quest.status, quest.emoji)}</span><small>{quest.category}</small><b>{quest.title}</b><em>+{quest.xp} XP</em></button>)}</div></>}</section>}
    {fullScreen && <div className={styles.expandedPanel}><p>{activeQuest ? activeQuest.place || activeQuest.title : homeLabel}</p>{!mapFailed && (activeQuest?.status === "offered" || activeQuest?.status === "active") && <>{selectedRouteMode && <><button type="button" className={styles.routeButton} onClick={requestRoute} disabled={routeStep === "loading"}>{routeLabel}</button>{routeStep === "consent" && <small>Your current GPS location is used only to calculate {routeModeCopy} to this quest.</small>}</>}{routeSummary && <div className={styles.routeSummary}>{metersText(routeSummary.distanceMeters)} · {minutesText(routeSummary.durationSeconds, selectedRouteMode ?? "walking")} <button type="button" onClick={clearRoute}>Clear</button></div>}</>}</div>}
    {message && <div role="status" className={styles.message}>{message}<button type="button" onClick={() => setMessage(null)} aria-label="Dismiss">×</button></div>}</section>;
}
