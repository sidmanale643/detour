export type QuestCategory = "Explore" | "Move" | "Create" | "Mind" | "Taste" | "Learn";
export type QuestStatus = "offered" | "active" | "completed" | "skipped" | "superseded" | "expired";
export type Motivation =
  | "explore"
  | "move"
  | "learn"
  | "create"
  | "reset"
  | "nature"
  | "break_routine";
export type MovementIntensity = "gentle" | "moderate" | "energetic";
export type TravelMode =
  | "walking"
  | "cycling"
  | "two_wheeler"
  | "four_wheeler"
  | "public_transport";
export type Budget = "free" | "low";
export type SocialComfort = "solo_only" | "optional_interaction";
export type EnvironmentPreference = "indoor" | "outdoor" | "either";
export type InterestAffinity = "love" | "okay" | "avoid";
export type QuestIntent = "explore" | "unwind" | "learn" | "create" | "move";
export type ActivityStyle = "wander" | "observe" | "photograph" | "sketch_or_write" | "solve_or_research" | "reflect" | "workout";
export type AccessibilityPreferences = {
  stepFree: boolean;
  wheelchairAccess: boolean;
  maxWalkingMinutes: number | null;
  seating: boolean;
  lowSensory: boolean;
  notes: string | null;
};

export type Quest = {
  id: string;
  title: string;
  place: string;
  distance: string;
  duration: string;
  walkingMinutes: number | null;
  activityMinutes: number | null;
  distanceSource: "google_routes" | "walking_route" | "approximate" | null;
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
  intent: QuestIntent | null;
  activityStyle: ActivityStyle | null;
  travelMode: TravelMode | null;
  oneWayTravelMinutes: number | null;
  totalEstimatedMinutes: number | null;
  matchReasons: string[];
};
export type Progress = {
  xp: number;
  level: number;
  streak: number;
  categories: Record<string, number>;
};
export type Coordinate = { latitude: number; longitude: number };
export type RoutePreview = {
  travelMode: TravelMode;
  distanceMeters: number;
  durationSeconds: number;
  encodedPolyline: string | null;
};
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
  username: string;
  email: string;
  emailVerified: boolean;
  timezone: string;
  likes: string[];
  dislikes: string[];
  categories: string[];
  motivations: Motivation[];
  availableMinutes: number | null;
  travelModes: TravelMode[];
  maxTravelMinutes: number | null;
  /** Legacy response field retained while older API clients migrate. */
  maxWalkingMinutes: number | null;
  movementIntensity: MovementIntensity;
  budget: Budget;
  socialComfort: SocialComfort;
  environmentPreference: EnvironmentPreference;
  accessibilityNotes: string | null;
  interestPreferences: Record<string, InterestAffinity>;
  customInterests: string[];
  primaryIntent: QuestIntent;
  secondaryIntents: QuestIntent[];
  activityStyles: ActivityStyle[];
  primaryTravelMode: TravelMode;
  fallbackTravelModes: TravelMode[];
  totalTimeMinutes: number | null;
  maxOneWayTravelMinutes: number | null;
  maxOneWayDistanceMetres: number | null;
  accessibility: AccessibilityPreferences;
  preferenceVersion: number;
  homeZone: HomeZone | null;
};
export type Deck = { quests: Quest[]; refreshAvailable: boolean };
export type DiscoveryPlace = {
  provider: "openstreetmap" | "wikidata" | "google_places";
  providerId: string;
  name: string;
  placeType: string;
  latitude: number;
  longitude: number;
  distanceMetres: number;
  tripKind: "nearby" | "city" | "day_trip";
  description: string | null;
  imageUrl: string | null;
  externalUrl: string | null;
  rating: number | null;
  reviewCount: number | null;
  openNow: boolean | null;
  cuisines: string[];
};
export type Discovery = {
  city: string;
  nearby: DiscoveryPlace[];
  cityHighlights: DiscoveryPlace[];
  dayTrips: DiscoveryPlace[];
  food: DiscoveryPlace[];
  foodAvailable: boolean;
};

export type PreferenceInput = {
  interestPreferences?: Record<string, InterestAffinity>;
  customInterests?: string[];
  primaryIntent?: QuestIntent;
  secondaryIntents?: QuestIntent[];
  activityStyles?: ActivityStyle[];
  primaryTravelMode?: TravelMode;
  fallbackTravelModes?: TravelMode[];
  totalTimeMinutes?: number | null;
  maxOneWayTravelMinutes?: number | null;
  maxOneWayDistanceMetres?: number | null;
  accessibility?: AccessibilityPreferences;
  likes?: string[];
  dislikes?: string[];
  categories?: string[];
  motivations?: Motivation[];
  availableMinutes?: number | null;
  travelModes?: TravelMode[];
  maxTravelMinutes?: number | null;
  /** Legacy request field retained for older callers. */
  maxWalkingMinutes?: number | null;
  movementIntensity?: MovementIntensity;
  budget?: Budget;
  socialComfort?: SocialComfort;
  environmentPreference?: EnvironmentPreference;
  accessibilityNotes?: string | null;
};

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") || "http://localhost:8000";
/** When true, the client may use the local dev-session bootstrap (no dummy quests). */
export const authDisabled = process.env.NEXT_PUBLIC_AUTH_DISABLED !== "false";
/** Always false: dummy/demo quests are removed. Kept for any UI that still checks the flag. */
export const isDemoMode = false;
const tokenKey = "detour.access-token";
const accents = ["coral", "aqua", "purple", "gold", "mint", "blue"];
const categoryMap: Record<string, { category: QuestCategory; emoji: string }> = {
  nature: { category: "Explore", emoji: "🌿" },
  culture: { category: "Explore", emoji: "🏛️" },
  creativity: { category: "Create", emoji: "✂️" },
  mindfulness: { category: "Mind", emoji: "☁️" },
  fitness: { category: "Move", emoji: "⚡" },
  learning: { category: "Learn", emoji: "📚" },
};

export const MOTIVATION_OPTIONS: { value: Motivation; label: string }[] = [
  { value: "explore", label: "Explore" },
  { value: "move", label: "Move" },
  { value: "learn", label: "Learn" },
  { value: "create", label: "Create" },
  { value: "reset", label: "Reset" },
  { value: "nature", label: "Nature" },
  { value: "break_routine", label: "Break routine" },
];

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

/** Derive quest categories for XP/classification from motivations. */
export function categoriesFromMotivations(motivations: Motivation[]): string[] {
  const map: Record<Motivation, string[]> = {
    explore: ["culture", "nature"],
    move: ["fitness"],
    learn: ["learning", "culture"],
    create: ["creativity"],
    reset: ["mindfulness"],
    nature: ["nature"],
    break_routine: ["culture", "nature", "mindfulness"],
  };
  const seen = new Set<string>();
  const result: string[] = [];
  for (const motivation of motivations) {
    for (const category of map[motivation] || []) {
      if (!seen.has(category)) {
        seen.add(category);
        result.push(category);
      }
    }
  }
  return result.length ? result : ["nature", "culture"];
}

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
  const walkingRaw = raw.walking_minutes ?? raw.walkingMinutes;
  const walkingMinutes =
    walkingRaw == null || walkingRaw === "" ? null : Number(walkingRaw);
  const activityRaw =
    raw.estimated_activity_minutes ?? raw.estimatedActivityMinutes;
  const activityMinutes =
    activityRaw == null || activityRaw === "" ? null : Number(activityRaw);
  const distanceSourceRaw = String(
    raw.distance_source ?? raw.distanceSource ?? ""
  );
  const distanceSource =
    distanceSourceRaw === "google_routes" ||
    distanceSourceRaw === "walking_route" ||
    distanceSourceRaw === "approximate"
      ? distanceSourceRaw
      : null;
  const durationParts: string[] = [];
  if (walkingMinutes != null && Number.isFinite(walkingMinutes)) {
    const travelLabel =
      distanceSource === "approximate"
        ? `~${walkingMinutes} min away`
        : `${walkingMinutes} min away`;
    durationParts.push(travelLabel);
  }
  if (activityMinutes != null && Number.isFinite(activityMinutes)) {
    durationParts.push(`${activityMinutes} min activity`);
  }
  const duration =
    durationParts.length > 0
      ? durationParts.join(" · ")
      : `${({ easy: 15, medium: 25, hard: 40 }[String(raw.difficulty)] || 25)} min`;
  const routeMinutesRaw = raw.one_way_travel_minutes ?? raw.travel_minutes ?? raw.walking_minutes;
  const routeMinutes = routeMinutesRaw == null ? null : Number(routeMinutesRaw);
  const totalMinutesRaw = raw.total_estimated_minutes ?? raw.total_duration_minutes;
  const totalMinutes = totalMinutesRaw == null ? null : Number(totalMinutesRaw);
  const travelMode = String(raw.travel_mode || "");
  const intent = String(raw.intent || "");
  const activityStyle = String(raw.activity_style || raw.activity_type || "");

  return {
    id: String(raw.id),
    title: String(raw.title),
    place: String(raw.place_name || "City spot"),
    distance: formatDistance(
      meters != null && Number.isFinite(meters) ? meters : null,
      distanceSource
    ),
    duration,
    walkingMinutes:
      walkingMinutes != null && Number.isFinite(walkingMinutes)
        ? walkingMinutes
        : null,
    activityMinutes:
      activityMinutes != null && Number.isFinite(activityMinutes)
        ? activityMinutes
        : null,
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
    intent: ["explore", "unwind", "learn", "create", "move"].includes(intent) ? intent as QuestIntent : null,
    activityStyle: ["wander", "observe", "photograph", "sketch_or_write", "solve_or_research", "reflect", "workout"].includes(activityStyle) ? activityStyle as ActivityStyle : null,
    travelMode: ["walking", "cycling", "two_wheeler", "four_wheeler", "public_transport"].includes(travelMode) ? travelMode as TravelMode : null,
    oneWayTravelMinutes: Number.isFinite(routeMinutes) ? routeMinutes : null,
    totalEstimatedMinutes: Number.isFinite(totalMinutes) ? totalMinutes : null,
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
  const motivations = ((raw.motivations as string[]) || []).filter(
    (value): value is Motivation =>
      [
        "explore",
        "move",
        "learn",
        "create",
        "reset",
        "nature",
        "break_routine",
      ].includes(value)
  );
  const intensity = String(raw.movement_intensity || "gentle");
  const rawTravelModes = Array.isArray(raw.travel_modes) ? raw.travel_modes : [];
  const travelModes = rawTravelModes.filter(
    (value): value is TravelMode =>
      [
        "walking",
        "cycling",
        "two_wheeler",
        "four_wheeler",
        "public_transport",
      ].includes(String(value))
  );
  const budget = String(raw.budget || "free");
  const social = String(raw.social_comfort || "solo_only");
  const environment = String(raw.environment_preference || "either");
  const affinitiesRaw = raw.interest_preferences as Record<string, unknown> | undefined;
  const interestPreferences = Object.fromEntries(Object.entries(affinitiesRaw || {}).flatMap(([key, value]) =>
    ["love", "okay", "avoid"].includes(String(value)) ? [[key, String(value) as InterestAffinity]] : []
  ));
  const primaryIntentValue = String(raw.primary_intent || motivations[0] || "explore");
  const primaryIntent = (["explore", "unwind", "learn", "create", "move"].includes(primaryIntentValue) ? primaryIntentValue : "explore") as QuestIntent;
  const secondaryIntents = Array.isArray(raw.secondary_intents) ? raw.secondary_intents.filter((value): value is QuestIntent => ["explore", "unwind", "learn", "create", "move"].includes(String(value)) && value !== primaryIntent) : [];
  const activityStyles = Array.isArray(raw.activity_styles) ? raw.activity_styles.filter((value): value is ActivityStyle => ["wander", "observe", "photograph", "sketch_or_write", "solve_or_research", "reflect", "workout"].includes(String(value))) : [];
  const primaryTravelModeRaw = String(raw.primary_travel_mode || travelModes[0] || "walking");
  const primaryTravelMode = (["walking", "cycling", "two_wheeler", "four_wheeler", "public_transport"].includes(primaryTravelModeRaw) ? primaryTravelModeRaw : "walking") as TravelMode;
  const fallbackTravelModes = Array.isArray(raw.fallback_travel_modes) ? raw.fallback_travel_modes.filter((value): value is TravelMode => ["walking", "cycling", "two_wheeler", "four_wheeler", "public_transport"].includes(String(value)) && value !== primaryTravelMode) : travelModes.filter((mode) => mode !== primaryTravelMode);
  const accessibilityRaw = raw.accessibility as Record<string, unknown> | undefined;
  const accessibility: AccessibilityPreferences = { stepFree: Boolean(accessibilityRaw?.step_free ?? accessibilityRaw?.stepFree), wheelchairAccess: Boolean(accessibilityRaw?.wheelchair_access ?? accessibilityRaw?.wheelchairAccess), maxWalkingMinutes: typeof (accessibilityRaw?.max_walking_minutes ?? accessibilityRaw?.maxWalkingMinutes) === "number" ? Number(accessibilityRaw?.max_walking_minutes ?? accessibilityRaw?.maxWalkingMinutes) : null, seating: Boolean(accessibilityRaw?.seating), lowSensory: Boolean(accessibilityRaw?.low_sensory ?? accessibilityRaw?.lowSensory), notes: typeof (accessibilityRaw?.notes) === "string" ? String(accessibilityRaw?.notes) : raw.accessibility_notes as string | null };
  return {
    username: String(raw.username),
    email: String(raw.email),
    emailVerified: Boolean(raw.email_verified),
    timezone: String(raw.timezone),
    likes: (raw.likes as string[]) || [],
    dislikes: (raw.dislikes as string[]) || [],
    categories: (raw.categories as string[]) || [],
    motivations,
    availableMinutes: (raw.available_minutes as number | null) ?? 30,
    travelModes: travelModes.length ? travelModes : ["walking"],
    maxTravelMinutes:
      (raw.max_travel_minutes as number | null)
      ?? (raw.max_walking_minutes as number | null)
      ?? 20,
    maxWalkingMinutes: (raw.max_walking_minutes as number | null) ?? 20,
    movementIntensity: (
      ["gentle", "moderate", "energetic"].includes(intensity)
        ? intensity
        : "gentle"
    ) as MovementIntensity,
    budget: (budget === "low" ? "low" : "free") as Budget,
    socialComfort: (
      social === "optional_interaction" ? "optional_interaction" : "solo_only"
    ) as SocialComfort,
    environmentPreference: (
      ["indoor", "outdoor", "either"].includes(environment)
        ? environment
        : "either"
    ) as EnvironmentPreference,
    accessibilityNotes: raw.accessibility_notes as string | null,
    interestPreferences,
    customInterests: Array.isArray(raw.custom_interests) ? raw.custom_interests.filter((value): value is string => typeof value === "string") : [],
    primaryIntent,
    secondaryIntents,
    activityStyles,
    primaryTravelMode,
    fallbackTravelModes,
    totalTimeMinutes: Number(raw.total_time_minutes ?? raw.available_minutes) || 30,
    maxOneWayTravelMinutes: Number(raw.max_one_way_travel_minutes ?? raw.max_travel_minutes) || 20,
    maxOneWayDistanceMetres: Number(raw.max_one_way_distance_metres) || 5_000,
    accessibility,
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
  private accessToken =
    typeof window === "undefined" ? null : localStorage.getItem(tokenKey);

  private async request(path: string, init: RequestInit = {}, retry = true) {
    const headers = new Headers(init.headers);
    if (this.accessToken) headers.set("Authorization", `Bearer ${this.accessToken}`);
    if (init.body) headers.set("Content-Type", "application/json");
    let response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });
    if (response.status === 401 && retry && path !== "/v1/auth/refresh" && path !== "/v1/auth/dev-session") {
      try {
        if (authDisabled) {
          await this.devSession();
        } else {
          await this.refresh();
        }
        response = (await this.request(path, init, false)) as Response;
      } catch {
        this.clearToken();
      }
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(
        String(payload.detail || payload.message || "Something went wrong")
      );
    }
    return response;
  }

  private saveToken(value: string) {
    this.accessToken = value;
    localStorage.setItem(tokenKey, value);
  }

  private clearToken() {
    this.accessToken = null;
    localStorage.removeItem(tokenKey);
  }

  async devSession() {
    const response = await this.request(
      "/v1/auth/dev-session",
      { method: "POST" },
      false
    );
    const data = await response.json();
    this.saveToken(data.access_token);
  }

  async register(input: {
    username: string;
    email: string;
    password: string;
    birthDate: string;
    timezone: string;
  }) {
    await this.request("/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({
        username: input.username,
        email: input.email,
        password: input.password,
        birth_date: input.birthDate,
        timezone: input.timezone,
      }),
    });
  }

  async login(username: string, password: string) {
    const response = await this.request("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    const data = await response.json();
    this.saveToken(data.access_token);
    return this.profile();
  }

  async refresh() {
    const response = await this.request("/v1/auth/refresh", { method: "POST" }, false);
    const data = await response.json();
    this.saveToken(data.access_token);
  }

  async logout() {
    try {
      await this.request("/v1/auth/logout", { method: "POST" });
    } finally {
      this.clearToken();
    }
  }

  async profile() {
    if (authDisabled && !this.accessToken) {
      try {
        await this.devSession();
      } catch {
        // Fall through; request will surface the error.
      }
    }
    return mapProfile(await (await this.request("/v1/profile")).json());
  }

  async verifyEmail() {
    await this.request("/v1/auth/verify-email", { method: "POST" });
  }

  async savePreferences(input: PreferenceInput) {
    const body: Record<string, unknown> = {};
    if (input.interestPreferences !== undefined) body.interest_preferences = input.interestPreferences;
    if (input.customInterests !== undefined) body.custom_interests = input.customInterests;
    if (input.primaryIntent !== undefined) body.primary_intent = input.primaryIntent;
    if (input.secondaryIntents !== undefined) body.secondary_intents = input.secondaryIntents;
    if (input.activityStyles !== undefined) body.activity_styles = input.activityStyles;
    if (input.primaryTravelMode !== undefined) body.primary_travel_mode = input.primaryTravelMode;
    if (input.fallbackTravelModes !== undefined) body.fallback_travel_modes = input.fallbackTravelModes;
    if (input.totalTimeMinutes !== undefined) body.total_time_minutes = input.totalTimeMinutes;
    if (input.maxOneWayTravelMinutes !== undefined) body.max_one_way_travel_minutes = input.maxOneWayTravelMinutes;
    if (input.maxOneWayDistanceMetres !== undefined) body.max_one_way_distance_metres = input.maxOneWayDistanceMetres;
    if (input.accessibility !== undefined) body.accessibility = { step_free: input.accessibility.stepFree, wheelchair_access: input.accessibility.wheelchairAccess, max_walking_minutes: input.accessibility.maxWalkingMinutes, seating: input.accessibility.seating, low_sensory: input.accessibility.lowSensory, notes: input.accessibility.notes };
    if (input.likes !== undefined) body.likes = input.likes;
    if (input.dislikes !== undefined) body.dislikes = input.dislikes;
    if (input.categories !== undefined) body.categories = input.categories;
    if (input.motivations !== undefined) body.motivations = input.motivations;
    if (input.availableMinutes !== undefined) {
      body.available_minutes = input.availableMinutes;
    }
    if (input.travelModes !== undefined) body.travel_modes = input.travelModes;
    if (input.maxTravelMinutes !== undefined) {
      body.max_travel_minutes = input.maxTravelMinutes;
    }
    if (input.maxWalkingMinutes !== undefined) {
      body.max_walking_minutes = input.maxWalkingMinutes;
    }
    if (input.movementIntensity !== undefined) {
      body.movement_intensity = input.movementIntensity;
    }
    if (input.budget !== undefined) body.budget = input.budget;
    if (input.socialComfort !== undefined) body.social_comfort = input.socialComfort;
    if (input.environmentPreference !== undefined) {
      body.environment_preference = input.environmentPreference;
    }
    if (input.accessibilityNotes !== undefined) {
      body.accessibility_notes = input.accessibilityNotes;
    }
    const response = await this.request("/v1/profile", {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    const raw = await response.json();
    return raw && typeof raw === "object" && ("username" in raw || "profile" in raw) ? mapProfile((raw.profile || raw) as Record<string, unknown>) : this.profile();
  }

  async routePreview(
    origin: Coordinate,
    destination: Coordinate,
    travelMode: TravelMode
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
        typeof data.encoded_polyline === "string"
          ? data.encoded_polyline
          : null,
    };
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

  async discover(foodQuery?: string): Promise<Discovery> {
    const params = new URLSearchParams();
    if (foodQuery?.trim()) params.set("food_query", foodQuery.trim());
    const suffix = params.size ? `?${params}` : "";
    const raw = (await (await this.request(`/v1/discover${suffix}`)).json()) as Record<string, unknown>;
    const mapPlace = (value: Record<string, unknown>): DiscoveryPlace => ({
      provider: value.provider === "wikidata" || value.provider === "google_places" ? value.provider : "openstreetmap",
      providerId: String(value.provider_id || ""),
      name: String(value.name || "Unnamed place"),
      placeType: String(value.place_type || "place").replaceAll("_", " "),
      latitude: Number(value.latitude), longitude: Number(value.longitude),
      distanceMetres: Number(value.distance_metres || 0),
      tripKind: value.trip_kind === "day_trip" || value.trip_kind === "city" ? value.trip_kind : "nearby",
      description: typeof value.description === "string" ? value.description : null,
      imageUrl: typeof value.image_url === "string" ? value.image_url : null,
      externalUrl: typeof value.external_url === "string" ? value.external_url : null,
      rating: typeof value.rating === "number" ? value.rating : null,
      reviewCount: typeof value.review_count === "number" ? value.review_count : null,
      openNow: typeof value.open_now === "boolean" ? value.open_now : null,
      cuisines: Array.isArray(value.cuisines) ? value.cuisines.filter((item): item is string => typeof item === "string") : [],
    });
    const places = (key: string) => Array.isArray(raw[key]) ? (raw[key] as Record<string, unknown>[]).map(mapPlace) : [];
    return {
      city: String(raw.city || "Your city"), nearby: places("nearby"),
      cityHighlights: places("city_highlights"), dayTrips: places("day_trips"),
      food: places("food"), foodAvailable: raw.food_available === true,
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
