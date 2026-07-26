"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Map as MapLibreMap, Marker } from "maplibre-gl";
import type { Coordinate } from "../lib/quest-api";
import { osmRasterStyle } from "../lib/osm-map";
import styles from "./AreaPickerMap.module.css";

export interface AreaPickerMapProps {
  selected: Coordinate | null;
  onSelect: (coordinate: Coordinate) => void;
  className?: string;
}

export default function AreaPickerMap({ selected, onSelect, className }: AreaPickerMapProps) {
  const initialCenter = useMemo(() => selected, [selected]);
  const selectedLongitude = selected?.longitude ?? null;
  const selectedLatitude = selected?.latitude ?? null;
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const markerRef = useRef<Marker | null>(null);
  const onSelectRef = useRef(onSelect);
  const [mapFailed, setMapFailed] = useState(false);
  const [mapReady, setMapReady] = useState(false);
  onSelectRef.current = onSelect;

  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current || !initialCenter) return;
    let cancelled = false;
    let map: MapLibreMap | null = null;
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      if (cancelled || !mapContainerRef.current) return;
      map = new maplibregl.Map({
        container: mapContainerRef.current,
        style: osmRasterStyle,
        center: [initialCenter.longitude, initialCenter.latitude],
        zoom: 11.5,
        pitch: 34,
        bearing: -12,
      });
      mapRef.current = map;
      map.on("load", () => { if (!cancelled) setMapReady(true); });
      map.on("click", (event) => onSelectRef.current({ latitude: event.lngLat.lat, longitude: event.lngLat.lng }));
      map.on("error", () => { if (!cancelled) setMapFailed(true); });
    }).catch(() => setMapFailed(true));
    return () => { cancelled = true; markerRef.current?.remove(); map?.remove(); mapRef.current = null; };
  }, [initialCenter]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map || selectedLongitude == null || selectedLatitude == null) return;
    void import("maplibre-gl").then(({ default: maplibregl }) => {
      markerRef.current?.remove();
      const element = document.createElement("div");
      element.className = styles.marker;
      element.setAttribute("aria-label", "Selected home zone");
      element.innerHTML = "<span>⌂</span>";
      const marker = new maplibregl.Marker({ element, anchor: "bottom", draggable: true }).setLngLat([selectedLongitude, selectedLatitude]).addTo(map);
      marker.on("dragend", () => {
        const point = marker.getLngLat();
        onSelectRef.current({ latitude: point.lat, longitude: point.lng });
      });
      markerRef.current = marker;
      map.easeTo({ center: [selectedLongitude, selectedLatitude], duration: 350 });
    });
  }, [mapReady, selectedLatitude, selectedLongitude]);

  return <section className={`${styles.shell} ${className ?? ""}`} aria-label="Choose your approximate home zone">
    {!initialCenter ? <div className={styles.empty}>Search for an address or use your live location to load the map.</div> : mapFailed ? <div className={styles.empty}>Map tiles are unavailable. Try again later.</div> : <div ref={mapContainerRef} className={styles.map} />}
    {initialCenter && <span className={styles.caption}>Drag the home marker if you need to adjust it.</span>}
  </section>;
}
