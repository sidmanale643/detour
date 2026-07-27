export type QuestCategory = "Explore" | "Move" | "Create" | "Mind" | "Taste" | "Learn";
export type QuestStatus = "offered" | "active" | "completed" | "skipped" | "superseded" | "expired";
export type MovementIntensity = "gentle" | "moderate" | "energetic";
export type InterestAffinity = "love" | "okay" | "avoid";
export type TravelMode =
  | "walking"
  | "cycling"
  | "two_wheeler"
  | "four_wheeler"
  | "public_transport";
export type RoutePreview = {
  travelMode: TravelMode;
  distanceMeters: number;
  durationSeconds: number;
  encodedPolyline: string | null;
};
export type Quest = {
  id: string;
  title: string;
  place: string;
  distance: string;
  distanceSource: "approximate" | null;
  xp: number;
  category: QuestCategory;
  emoji: string;
  accent: string;
  status: QuestStatus;
  startedAt: string | null;
  startExpiresAt: string | null;
  time: string;
  detail: string;
  latitude?: number;
  longitude?: number;
  coordinates?: [longitude: number, latitude: number];
  topic: string | null;
  matchReasons: string[];
};
export type Progress = {
  xp: number;
  level: number;
  streak: number;
  categories: Record<string, number>;
};
export type Coordinate = { latitude: number; longitude: number };
export type HomeZone = {
  city: string;
  address: string;
  source: "address" | "live_location";
  h3Cell: string;
  center: Coordinate | null;
};
export type AreaCandidate = {
  city: string;
  name: string;
  latitude: number;
  longitude: number;
};
export type Profile = {
  timezone: string;
  movementIntensity: MovementIntensity;
  interestPreferences: Record<string, InterestAffinity>;
  customInterests: string[];
  maxOneWayDistanceMetres: number | null;
  preferenceVersion: number;
  homeZone: HomeZone | null;
};
export type Deck = { quests: Quest[]; refreshAvailable: boolean };
export type DiscoveryPlace = {
  provider: "openstreetmap";
  providerId: string;
  name: string;
  placeType: string;
  matchingInterest: string;
  latitude: number;
  longitude: number;
  distanceMetres: number;
  description: string | null;
  externalUrl: string | null;
};
export type Discovery = {
  city: string;
  matches: DiscoveryPlace[];
};

export type PreferenceInput = {
  interestPreferences?: Record<string, InterestAffinity>;
  customInterests?: string[];
  maxOneWayDistanceMetres?: number | null;
  movementIntensity?: MovementIntensity;
};

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") || "http://localhost:8000";
const accents = ["coral", "aqua", "purple", "gold", "mint", "blue"];
const categoryMap: Record<string, { category: QuestCategory; emoji: string }> = {
  explorer: { category: "Explore", emoji: "🏛️" },
  foodie: { category: "Taste", emoji: "🍜" },
  skill_builder: { category: "Learn", emoji: "📚" },
  social_connector: { category: "Explore", emoji: "✨" },
  adventurer: { category: "Move", emoji: "⚡" },
  nature_mindfulness: { category: "Mind", emoji: "🌿" },
};

export const INTEREST_CHIPS = [
  "architecture",
  "history",
  "parks",
  "art",
  "books",
  "music",
  "food smells",
  "street life",
  "quiet corners",
  "trees",
  "photography spots",
  "local culture",
];

function formatDistance(
  meters: number | null | undefined,
  source: string | null | undefined
): string {
  if (meters == null || !Number.isFinite(meters) || meters <= 0) return "Nearby";
  const rounded =
    meters < 1000
      ? `${Math.round(meters / 10) * 10} m`
      : `${(meters / 1000).toFixed(1)} km`;
  if (source === "approximate") return `~${rounded}`;
  return rounded;
}

function mapQuest(raw: Record<string, unknown>, index = 0): Quest {
  const categoryValue = String(raw.category || "Explore").toLowerCase();
  const visual = categoryMap[categoryValue] || {
    category: "Explore" as QuestCategory,
    emoji: "✦",
  };
  const start = raw.time_window_start as string | null;
  const end = raw.time_window_end as string | null;
  const latitude = Number(raw.latitude);
  const longitude = Number(raw.longitude);
  const metersRaw = raw.distance_meters ?? raw.distanceMeters;
  const meters =
    metersRaw == null || metersRaw === ""
      ? null
      : Number(metersRaw);
  const distanceSourceRaw = String(
    raw.distance_source ?? raw.distanceSource ?? ""
  );
  const distanceSource = distanceSourceRaw === "approximate" ? distanceSourceRaw : null;

  return {
    id: String(raw.id),
    title: String(raw.title),
    place: String(raw.place_name || "City spot"),
    distance: formatDistance(
      meters != null && Number.isFinite(meters) ? meters : null,
      distanceSource
    ),
    distanceSource,
    xp: Number(raw.base_xp || 0),
    category: visual.category,
    emoji: visual.emoji,
    accent: accents[index % accents.length],
    status: String(raw.state || "offered") as QuestStatus,
    startedAt: raw.started_at ? String(raw.started_at) : null,
    startExpiresAt: raw.start_expires_at ? String(raw.start_expires_at) : null,
    time: start && end ? `${start} – ${end}` : "Anytime",
    detail: String(raw.description || "A small invitation to explore your city."),
    topic: raw.topic ? String(raw.topic) : raw.category ? String(raw.category) : null,
    matchReasons: Array.isArray(raw.match_reasons) ? raw.match_reasons.filter((value): value is string => typeof value === "string") : [],
    ...(Number.isFinite(latitude) && Number.isFinite(longitude)
      ? { latitude, longitude, coordinates: [longitude, latitude] as [number, number] }
      : {}),
  };
}

function mapProgress(raw: Record<string, unknown>): Progress {
  return {
    xp: Number(raw.total_xp ?? raw.xp ?? 0),
    level: Number(raw.level ?? 1),
    streak: Number(raw.streak ?? 0),
    categories: (raw.categories as Record<string, number>) || {},
  };
}

function mapProfile(raw: Record<string, unknown>): Profile {
  const home = raw.home_zone as Record<string, unknown> | null;
  const center = home?.center as Record<string, unknown> | undefined;
  const latitude = Number(center?.latitude);
  const longitude = Number(center?.longitude);
  const intensity = String(raw.movement_intensity || "gentle");
  const affinitiesRaw = raw.interest_preferences as Record<string, unknown> | undefined;
  const interestPreferences = Object.fromEntries(Object.entries(affinitiesRaw || {}).flatMap(([key, value]) =>
    ["love", "okay", "avoid"].includes(String(value)) ? [[key, String(value) as InterestAffinity]] : []
  ));
  return {
    timezone: String(raw.timezone),
    movementIntensity: (
      ["gentle", "moderate", "energetic"].includes(intensity)
        ? intensity
        : "gentle"
    ) as MovementIntensity,
    interestPreferences,
    customInterests: Array.isArray(raw.custom_interests) ? raw.custom_interests.filter((value): value is string => typeof value === "string") : [],
    maxOneWayDistanceMetres: Number(raw.max_one_way_distance_metres) || 5_000,
    preferenceVersion: Number(raw.preference_version || 1),
    homeZone: home
      ? {
          city: String(home.city),
          address: String(home.address || home.city),
          source: home.source === "live_location" ? "live_location" : "address",
          h3Cell: String(home.h3_cell),
          center:
            Number.isFinite(latitude) && Number.isFinite(longitude)
              ? { latitude, longitude }
              : null,
        }
      : null,
  };
}

class HttpQuestApi {
  private async request(path: string, init: RequestInit = {}) {
    const headers = new Headers(init.headers);
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(
        String(payload.detail || payload.message || "Something went wrong")
      );
    }
    return response;
  }

  async profile() {
    return mapProfile(await (await this.request("/v1/profile")).json());
  }

  async savePreferences(input: PreferenceInput) {
    const body: Record<string, unknown> = {};
    if (input.interestPreferences !== undefined) body.interest_preferences = input.interestPreferences;
    if (input.customInterests !== undefined) body.custom_interests = input.customInterests;
    if (input.maxOneWayDistanceMetres !== undefined) body.max_one_way_distance_metres = input.maxOneWayDistanceMetres;
    if (input.movementIntensity !== undefined) {
      body.movement_intensity = input.movementIntensity;
    }
    const response = await this.request("/v1/profile", {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    const raw = await response.json();
    return raw && typeof raw === "object" && ("timezone" in raw || "profile" in raw) ? mapProfile((raw.profile || raw) as Record<string, unknown>) : this.profile();
  }

  async setHomeZone(input: {
    city: string;
    address: string;
    source: "address" | "live_location";
    latitude: number;
    longitude: number;
  }): Promise<HomeZone> {
    const response = await this.request("/v1/profile/home-zone", {
      method: "PUT",
      body: JSON.stringify(input),
    });
    const raw = (await response.json()) as Record<string, unknown>;
    const center = raw.center as Record<string, unknown>;
    return {
      city: String(raw.city),
      address: String(raw.address),
      source: raw.source === "live_location" ? "live_location" : "address",
      h3Cell: String(raw.h3_cell),
      center: {
        latitude: Number(center.latitude),
        longitude: Number(center.longitude),
      },
    };
  }

  async searchAreas(query: string, city?: string): Promise<AreaCandidate[]> {
    const params = new URLSearchParams({ q: query });
    if (city) params.set("city", city);
    const data = await (await this.request(`/v1/map/areas?${params}`)).json();
    return (data.areas || data)
      .map((area: Record<string, unknown>) => ({
        city: String(area.city || city || ""),
        name: String(area.name || area.place_name || area.city || "Selected area"),
        latitude: Number(area.latitude),
        longitude: Number(area.longitude),
      }))
      .filter(
        (area: AreaCandidate) =>
          Number.isFinite(area.latitude) && Number.isFinite(area.longitude)
      );
  }

  async discover(): Promise<Discovery> {
    const raw = (await (await this.request("/v1/discover")).json()) as Record<string, unknown>;
    const mapPlace = (value: Record<string, unknown>): DiscoveryPlace => ({
      provider: "openstreetmap",
      providerId: String(value.provider_id || ""),
      name: String(value.name || "Unnamed place"),
      placeType: String(value.place_type || "place").replaceAll("_", " "),
      matchingInterest: String(value.matching_interest || "Selected interest").replaceAll("_", " "),
      latitude: Number(value.latitude), longitude: Number(value.longitude),
      distanceMetres: Number(value.distance_metres || 0),
      description: typeof value.description === "string" ? value.description : null,
      externalUrl: typeof value.external_url === "string" ? value.external_url : null,
    });
    return {
      city: String(raw.city || "Your city"),
      matches: Array.isArray(raw.matches) ? (raw.matches as Record<string, unknown>[]).map(mapPlace) : [],
    };
  }

  async today(): Promise<Deck> {
    const data = await (await this.request("/v1/decks/today")).json();
    return {
      quests: data.quests.map(mapQuest),
      refreshAvailable: data.refresh_available,
    };
  }

  async generateDeck(): Promise<Deck> {
    const data = await (
      await this.request("/v1/decks/today/generate", { method: "POST" })
    ).json();
    return {
      quests: data.quests.map(mapQuest),
      refreshAvailable: data.refresh_available,
    };
  }

  async progressSummary() {
    return mapProgress(await (await this.request("/v1/progress")).json());
  }

  async complete(id: string) {
    const response = await this.request(`/v1/quests/${id}/complete`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    const data = await response.json();
    return {
      quest: mapQuest(data.quest),
      progress: mapProgress(data.progress),
      awardedXp: data.awarded_xp as number,
    };
  }

  async start(id: string): Promise<Quest> {
    const data = await (
      await this.request(`/v1/quests/${id}/start`, { method: "POST" })
    ).json();
    return mapQuest(data);
  }

  async routePreview(
    origin: Coordinate,
    destination: Coordinate,
    travelMode: TravelMode = "walking"
  ): Promise<RoutePreview> {
    const data = await (
      await this.request("/v1/routes/preview", {
        method: "POST",
        body: JSON.stringify({
          origin,
          destination,
          travel_mode: travelMode,
        }),
      })
    ).json();
    return {
      travelMode: data.travel_mode as TravelMode,
      distanceMeters: Number(data.distance_meters),
      durationSeconds: Number(data.duration_seconds),
      encodedPolyline:
        typeof data.encoded_polyline === "string" ? data.encoded_polyline : null,
    };
  }

  async skip(id: string) {
    await this.request(`/v1/quests/${id}/skip`, { method: "POST" });
  }

  async refreshDeck(): Promise<Deck> {
    const data = await (
      await this.request("/v1/decks/today/refresh", { method: "POST" })
    ).json();
    return {
      quests: data.quests.map(mapQuest),
      refreshAvailable: data.refresh_available,
    };
  }
}

export const questApi = new HttpQuestApi();
