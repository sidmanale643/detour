"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  authDisabled,
  questApi,
} from "../lib/quest-api";
import AreaPickerMap from "../components/AreaPickerMap";
import QuestMap from "../components/QuestMap";
import type {
  AccessibilityPreferences,
  ActivityStyle,
  AreaCandidate,
  Budget,
  Discovery,
  DiscoveryPlace,
  EnvironmentPreference,
  Profile,
  QuestIntent,
  Progress,
  Quest,
  SocialComfort,
  TravelMode,
} from "../lib/quest-api";

type Tab = "map" | "discover" | "quests" | "me";
type SetupStep = "home" | "interests" | "style" | "limits";

const INTEREST_OPTIONS = [
  ["nature_outdoors", "Nature and outdoors"], ["history_heritage", "History and heritage"],
  ["architecture_public_spaces", "Architecture and public spaces"], ["art_design", "Art and design"],
  ["books_learning", "Books and learning"], ["local_culture_community", "Local culture and community"],
  ["food_markets", "Food and markets"], ["music_performance", "Music and performance"],
] as const;
const INTENT_OPTIONS: { value: QuestIntent; label: string }[] = [{ value: "explore", label: "Explore" }, { value: "unwind", label: "Unwind" }, { value: "learn", label: "Learn" }, { value: "create", label: "Create" }, { value: "move", label: "Move" }];
const ACTIVITY_OPTIONS: { value: ActivityStyle; label: string }[] = [{ value: "wander", label: "Wander" }, { value: "observe", label: "Observe" }, { value: "photograph", label: "Photograph" }, { value: "sketch_or_write", label: "Sketch or write" }, { value: "solve_or_research", label: "Solve or research" }, { value: "reflect", label: "Reflect" }, { value: "workout", label: "Workout" }];

const TRAVEL_MODE_OPTIONS: { value: TravelMode; label: string; icon: string }[] = [
  { value: "walking", label: "Walking", icon: "🚶" },
  { value: "cycling", label: "Cycling", icon: "🚲" },
  { value: "two_wheeler", label: "2-wheeler", icon: "🛵" },
  { value: "four_wheeler", label: "4-wheeler", icon: "🚗" },
  { value: "public_transport", label: "Public transport", icon: "🚌" },
];

const localExplorerProfile: Profile = {
  username: "explorer",
  email: "local@detour.dev",
  emailVerified: true,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata",
  likes: [],
  dislikes: [],
  categories: ["nature", "culture"],
  motivations: ["explore"],
  availableMinutes: 30,
  travelModes: ["walking"],
  maxTravelMinutes: 20,
  maxWalkingMinutes: 20,
  movementIntensity: "gentle",
  budget: "free",
  socialComfort: "solo_only",
  environmentPreference: "either",
  accessibilityNotes: null,
  interestPreferences: { nature_outdoors: "love", local_culture_community: "love" },
  customInterests: [],
  primaryIntent: "explore",
  secondaryIntents: [],
  activityStyles: ["wander", "observe"],
  primaryTravelMode: "walking",
  fallbackTravelModes: [],
  totalTimeMinutes: 30,
  maxOneWayTravelMinutes: 20,
  maxOneWayDistanceMetres: 5_000,
  accessibility: { stepFree: false, wheelchairAccess: false, maxWalkingMinutes: null, seating: false, lowSensory: false, notes: null },
  preferenceVersion: 1,
  homeZone: null,
};

export default function DetourApp() {
  const [quests, setQuests] = useState<Quest[]>([]);
  const [progress, setProgress] = useState<Progress>({ xp: 0, level: 1, streak: 0, categories: {} });
  const [active, setActive] = useState<Quest | null>(null);
  const [tab, setTab] = useState<Tab>("map");
  const [refreshAvailable, setRefreshAvailable] = useState(true);
  const [pending, setPending] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [questError, setQuestError] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [booted, setBooted] = useState(false);
  const [editingPreferences, setEditingPreferences] = useState(false);
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);

  const loadDiscovery = useCallback(async (foodQuery?: string) => {
    setDiscovering(true);
    setDiscoverError(null);
    try {
      setDiscovery(await questApi.discover(foodQuery));
    } catch (error) {
      setDiscoverError(error instanceof Error ? error.message : "Could not discover places right now.");
    } finally {
      setDiscovering(false);
    }
  }, []);

  useEffect(() => {
    questApi.profile()
      .then(setProfile)
      .catch(() => {
        if (authDisabled) setProfile(localExplorerProfile);
      })
      .finally(() => setBooted(true));
  }, []);
  useEffect(() => {
    if (profile?.emailVerified && profile.homeZone) {
      questApi.progressSummary().then(setProgress).catch(() => undefined);
    }
  }, [profile]);
  const completed = useMemo(() => quests.filter((q) => q.status === "completed").length, [quests]);
  const sync = useCallback(async () => { const deck = await questApi.today(); setQuests([...deck.quests]); setRefreshAvailable(deck.refreshAvailable); }, []);
  const start = async (quest: Quest) => {
    if (pending || quest.status !== "offered") return;
    setPending(quest.id);
    setQuestError(null);
    setToast("Starting your one-hour quest…");
    try {
      await questApi.start(quest.id);
      await sync();
      // The running quest remains in the synced deck. Close the sheet so the
      // player immediately sees the map and its GPS route.
      setActive(null);
      setTab("map");
      setToast("Quest started · 1 hour on the clock");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not start this quest. Try again.";
      setQuestError(message);
      setToast(message);
    } finally {
      setPending(null);
    }
  };
  const complete = async (quest: Quest) => {
    if (pending || quest.status !== "active") return;
    setPending(quest.id); setToast("Completion pending sync");
    try {
      const result = await questApi.complete(quest.id);
      setProgress(result.progress); await sync(); setActive(result.quest || null); setToast(`+${quest.xp} XP earned`); window.setTimeout(() => setToast(null), 2200);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not complete this quest. Try again.";
      setQuestError(message);
      setToast(message);
    } finally {
      setPending(null);
    }
  };
  const skip = async (quest: Quest) => {
    try {
      await questApi.skip(quest.id);
      await sync();
      setActive(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not dismiss this quest. Try again.";
      setQuestError(message);
      setToast(message);
    }
  };
  const expireActiveQuest = useCallback(async (questId: string) => {
    setActive((current) => current?.id === questId ? null : current);
    setToast("Time is up. This quest has expired.");
    try {
      await sync();
    } catch {
      // The local sheet is closed even if a transient sync fails.
    }
  }, [sync]);
  useEffect(() => {
    const running = quests.find((quest) => quest.status === "active" && quest.startExpiresAt);
    if (!running?.startExpiresAt) return;
    const delay = Math.max(0, new Date(running.startExpiresAt).getTime() - Date.now());
    const timeout = window.setTimeout(() => { void expireActiveQuest(running.id); }, delay);
    return () => window.clearTimeout(timeout);
  }, [expireActiveQuest, quests]);
  const refresh = async () => {
    setQuestError(null);
    try {
      const deck = await questApi.refreshDeck();
      setQuests([...deck.quests]);
      setRefreshAvailable(deck.refreshAvailable);
      setToast("Your city deck has been refreshed");
      window.setTimeout(() => setToast(null), 2200);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not refresh quests. Try again.";
      setQuestError(message);
      setToast(message);
    }
  };
  const generate = async () => {
    if (generating) return;
    setGenerating(true);
    setQuestError(null);
    setToast("Finding quests near you…");
    try {
      const deck = await questApi.generateDeck();
      setQuests([...deck.quests]);
      setRefreshAvailable(deck.refreshAvailable);
      setToast(deck.quests.length ? "Your quests are ready" : "No quests were generated. Try again.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not generate quests. Try again.";
      setQuestError(message);
      setToast(message);
    } finally {
      setGenerating(false);
      window.setTimeout(() => setToast(null), 3200);
    }
  };

  if (!booted) return <main className="app-shell splash"><span className="brand-mark">✦</span><b>Detour</b></main>;
  if (!profile) return <Onboarding onReady={setProfile} />;
  if (!profile.emailVerified || !profile.homeZone) return <Setup profile={profile} onUpdate={setProfile} />;
  if (editingPreferences) {
    return (
      <Setup
        profile={profile}
        initialStep="interests"
        preferenceOnly
        onUpdate={(updatedProfile) => {
          setProfile(updatedProfile);
          setEditingPreferences(false);
          void sync().catch((error) => {
            setQuestError(
              error instanceof Error
                ? error.message
                : "Preferences saved, but the updated deck could not be loaded."
            );
          });
        }}
      />
    );
  }

  return <main className={`app-shell ${tab === "map" ? "map-app" : ""}`}>
    <header className="topbar"><div className="brand"><span className="brand-mark">✦</span><span>Detour</span></div><button className="avatar" onClick={() => setTab("me")}>S</button></header>
    {questError && <div className="quest-error" role="alert"><span>{questError}</span><button type="button" onClick={generate}>Try again</button></div>}
    {tab === "map" && <QuestMap quests={quests} activeQuest={active ?? quests.find((quest) => quest.status === "active") ?? null} onSelectQuest={setActive} homeCenter={profile.homeZone.center} homeLabel={profile.homeZone.city} level={progress.level} xp={progress.xp} completedCount={completed} dateLabel={new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: profile.timezone }).format(new Date()).toUpperCase()} refreshAvailable={refreshAvailable} onRefresh={refresh} onGenerate={generate} generating={generating} travelModes={[profile.primaryTravelMode, ...profile.fallbackTravelModes]} />}
    {tab === "discover" && <DiscoverPage city={profile.homeZone.city} discovery={discovery} loading={discovering} error={discoverError} onLoad={loadDiscovery} />}
    {tab === "quests" && <QuestList quests={quests} onSelect={setActive} onGenerate={generate} generating={generating} />}
    {tab === "me" && <UserProfile progress={progress} completed={completed} onEditPreferences={() => setEditingPreferences(true)} onSignOut={authDisabled ? undefined : async () => { await questApi.logout(); setProfile(null); }} />}
    <nav className="bottom-nav"><button className={tab === "map" ? "selected" : ""} onClick={() => setTab("map")}><span>⌖</span>Map</button><button className={tab === "discover" ? "selected" : ""} onClick={() => { setTab("discover"); if (!discovery && !discovering) void loadDiscovery(); }}><span>✦</span>Discover</button><button className={tab === "quests" ? "selected" : ""} onClick={() => setTab("quests")}><span>☷</span>Quests</button><button className={tab === "me" ? "selected" : ""} onClick={() => setTab("me")}><span>◉</span>Me</button></nav>
    {active && <QuestSheet quest={active} pending={pending === active.id} onClose={() => setActive(null)} onStart={() => start(active)} onComplete={() => complete(active)} onSkip={() => skip(active)} onExpired={() => expireActiveQuest(active.id)} />}
    {toast && <div className="toast">{toast}</div>}
  </main>;
}

function DiscoverPage({ city, discovery, loading, error, onLoad }: { city: string; discovery: Discovery | null; loading: boolean; error: string | null; onLoad: (foodQuery?: string) => Promise<void> }) {
  const [foodQuery, setFoodQuery] = useState("");
  const submit = (event: React.FormEvent<HTMLFormElement>) => { event.preventDefault(); void onLoad(foodQuery); };
  return <section className="discover-page">
    <p className="eyebrow">LIVE CITY GUIDE</p>
    <h1>find your next<br /><i>side quest.</i></h1>
    <p className="discover-copy">Parks, landmarks, day trips, and food in {discovery?.city || city}.</p>
    <form className="food-search" onSubmit={submit}>
      <input value={foodQuery} onChange={(event) => setFoodQuery(event.target.value)} placeholder="Try Naan Qalia, tahari, kebabs…" aria-label="Search a local dish" />
      <button disabled={loading} type="submit">{loading ? "Searching…" : "Find food"}</button>
    </form>
    {error && <div className="discover-error" role="alert">{error}<button type="button" onClick={() => void onLoad(foodQuery)}>Try again</button></div>}
    {loading && !discovery && <p className="discover-status">Finding real places…</p>}
    {discovery && <>
      <DiscoverSection title="City highlights" subtitle="Landmarks with real local stories" places={discovery.cityHighlights} empty="No verified city highlights were available right now." />
      <DiscoverSection title="Nearby outdoors" subtitle="Parks, gardens, and places to wander" places={discovery.nearby.filter((place) => !["restaurant", "cafe", "fast_food", "food_court"].includes(place.placeType.replaceAll(" ", "_")))} empty="No nearby public places were found." />
      <DiscoverSection title="Regional day trips" subtitle="Worth making a day of it" places={discovery.dayTrips} empty="No regional day trips were available right now." />
      <DiscoverSection title={foodQuery.trim() ? `Food for ${foodQuery.trim()}` : "Popular food"} subtitle="Live venue details from Google Places" places={discovery.food} empty={discovery.foodAvailable ? "No matching food venues were found. Try another dish." : "Food discovery needs a Google Places key on the server."} />
    </>}
  </section>;
}

function DiscoverSection({ title, subtitle, places, empty }: { title: string; subtitle: string; places: DiscoveryPlace[]; empty: string }) {
  return <section className="discover-section"><div><h2>{title}</h2><p>{subtitle}</p></div>{places.length ? <div className="discover-cards">{places.map((place) => <DiscoverCard key={`${place.provider}:${place.providerId}`} place={place} />)}</div> : <p className="discover-empty">{empty}</p>}</section>;
}

function DiscoverCard({ place }: { place: DiscoveryPlace }) {
  const distance = place.distanceMetres < 1000 ? `${Math.round(place.distanceMetres / 10) * 10} m` : `${(place.distanceMetres / 1000).toFixed(1)} km`;
  const content = <><span className="discover-kind">{place.tripKind === "day_trip" ? "DAY TRIP" : place.placeType}</span><b>{place.name}</b>{place.description && <em>{place.description}</em>}<small>{distance} away · {place.provider.replace("_", " ")}{place.rating != null && ` · ★ ${place.rating}${place.reviewCount != null ? ` (${place.reviewCount})` : ""}`}{place.openNow != null && ` · ${place.openNow ? "Open now" : "Closed"}`}</small></>;
  return place.externalUrl ? <a className="discover-card" href={place.externalUrl} target="_blank" rel="noreferrer">{content}</a> : <article className="discover-card">{content}</article>;
}

function QuestList({ quests, onSelect, onGenerate, generating }: { quests: Quest[]; onSelect: (quest: Quest) => void; onGenerate: () => void; generating: boolean }) {
  return (
    <section className="list-page">
      <p className="eyebrow">YOUR DAILY DECK</p>
      <h1>One chance to<br /><i>go somewhere.</i></h1>
      {quests.length === 0 ? (
        <button className="complete-button" disabled={generating} onClick={onGenerate}>
          {generating ? "Generating quests…" : "Generate quests"}
        </button>
      ) : (
        quests.map((q) => (
          <button className="list-quest" onClick={() => onSelect(q)} key={q.id}>
            <span className={`mini-icon ${q.accent}`}>{q.status === "completed" ? "✓" : q.emoji}</span>
            <span>
              <small>{q.topic || q.category} · {q.oneWayTravelMinutes != null ? `${q.oneWayTravelMinutes} min one way` : q.distance}</small>
              <b>{q.title}</b>
              {q.matchReasons.length > 0 && <em className="match-reasons">{q.matchReasons.join(" · ")}</em>}
              <em>{q.status === "completed" ? "Completed" : q.status === "active" ? "In progress · 1 hour" : q.status === "skipped" || q.status === "expired" ? "Unavailable" : "Begin quest"}</em>
            </span>
            <strong>›</strong>
          </button>
        ))
      )}
    </section>
  );
}

function UserProfile({ progress, completed, onEditPreferences, onSignOut }: { progress: Progress; completed: number; onEditPreferences: () => void; onSignOut?: () => void }) {
  return (
    <section className="profile-page">
      <div className="profile-hero">
        <div className="big-avatar">S</div>
        <p className="eyebrow">CITY EXPLORER</p>
        <h1>your <i>trail</i></h1>
        <span className="streak">✦ {progress.streak} day streak</span>
      </div>
      <div className="stat-row">
        <div><b>{progress.level}</b><span>level</span></div>
        <div><b>{progress.xp}</b><span>total XP</span></div>
        <div><b>{completed}</b><span>today</span></div>
      </div>
      <h2>Your tracks</h2>
      {Object.entries(progress.categories).map(([name, xp]) => (
        <div className="track" key={name}>
          <span>{name}</span>
          <div><i style={{ width: `${Math.min(100, xp / 2)}%` }} /></div>
          <b>{xp} XP</b>
        </div>
      ))}
      <button className="settings" onClick={onEditPreferences}>⚙ Quest preferences <span>›</span></button>
      {onSignOut && <button className="settings" onClick={onSignOut}>↪ Sign out <span>›</span></button>}
    </section>
  );
}

function formatRemaining(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.ceil(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function QuestTimer({ deadline, onExpired }: { deadline: string | null; onExpired: () => void }) {
  const [remaining, setRemaining] = useState(() => deadline ? new Date(deadline).getTime() - Date.now() : 0);
  const expiredCallback = useRef(onExpired);
  expiredCallback.current = onExpired;
  useEffect(() => {
    if (!deadline) return;
    let notified = false;
    const tick = () => {
      const next = new Date(deadline).getTime() - Date.now();
      setRemaining(next);
      if (next <= 0 && !notified) {
        notified = true;
        expiredCallback.current();
      }
    };
    tick();
    const interval = window.setInterval(tick, 1000);
    return () => window.clearInterval(interval);
  }, [deadline]);
  return <span className="quest-timer" role="timer" aria-label={`${formatRemaining(remaining)} remaining`}>⏱ {formatRemaining(remaining)}</span>;
}

function QuestSheet({ quest, pending, onClose, onStart, onComplete, onSkip, onExpired }: { quest: Quest; pending: boolean; onClose: () => void; onStart: () => void; onComplete: () => void; onSkip: () => void; onExpired: () => void }) {
  const done = quest.status === "completed";
  const travelLabel =
    quest.walkingMinutes != null
      ? quest.distanceSource === "approximate"
        ? `~${quest.walkingMinutes} min away`
        : `${quest.walkingMinutes} min away`
      : null;
  const activityLabel =
    quest.activityMinutes != null ? `${quest.activityMinutes} min activity` : null;
  return (
    <div className="sheet-backdrop" onMouseDown={onClose}>
      <section className="sheet" onMouseDown={(e) => e.stopPropagation()}>
        <div className="handle" />
        <button className="close" onClick={onClose}>×</button>
        <span className={`sheet-icon ${quest.accent}`}>{done ? "✓" : quest.emoji}</span>
        <p className="eyebrow">{quest.category.toUpperCase()} QUEST</p>
        <h2>{quest.title}</h2>
        <p className="place">
          ⌖ {quest.place} <span>·</span> {quest.distance}
          {quest.distanceSource === "approximate" ? " (approx.)" : ""}
        </p>
        <p className="description">{quest.detail}</p>
        {(quest.topic || quest.intent || quest.activityStyle) && <p className="quest-metadata">{[quest.topic, quest.intent, quest.activityStyle?.replaceAll("_", " ")].filter(Boolean).join(" · ")}</p>}
        {quest.matchReasons.length > 0 && <div className="match-reasons">{quest.matchReasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
        <div className="quest-meta">
          {travelLabel && <span>⌁ {travelLabel}</span>}
          {quest.oneWayTravelMinutes != null && <span>⌁ {quest.oneWayTravelMinutes} min one way{quest.totalEstimatedMinutes != null ? ` · ${quest.totalEstimatedMinutes} min total` : ""}</span>}
          {activityLabel && <span>◷ {activityLabel}</span>}
          {!travelLabel && !activityLabel && <span>◷ {quest.duration}</span>}
          <span>◒ {quest.time}</span>
          {quest.status === "active" && <QuestTimer deadline={quest.startExpiresAt} onExpired={onExpired} />}
          <b>+{quest.xp} XP</b>
        </div>
        {done ? (
          <button className="completed-button" disabled>✓ Quest completed</button>
        ) : quest.status === "skipped" ? (
          <button className="skipped-button" disabled>Quest unavailable</button>
        ) : quest.status === "expired" ? (
          <button className="skipped-button" disabled>Quest timer expired</button>
        ) : quest.status === "active" ? (
          <>
            <button className="complete-button" disabled={pending} onClick={onComplete}>
              {pending ? "Syncing completion…" : "✓ Completed"}
            </button>
            <button className="skip-button" onClick={onSkip}>Stop quest</button>
          </>
        ) : (
          <>
            <button className="complete-button" disabled={pending} onClick={onStart}>
              {pending ? "Starting quest…" : "▶ Begin quest · 1 hour"}
            </button>
            <button className="skip-button" onClick={onSkip}>Quest unavailable</button>
          </>
        )}
        <p className="honor-note">Completion is on the honor system. Check local conditions before you go.</p>
      </section>
    </div>
  );
}

function Onboarding({ onReady }: { onReady: (profile: Profile) => void }) {
  const [register, setRegister] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (form: FormData) => {
    setBusy(true);
    setError("");
    try {
      const username = String(form.get("username") || "");
      const password = String(form.get("password") || "");
      if (register) {
        await questApi.register({
          username,
          email: String(form.get("email")),
          password,
          birthDate: String(form.get("birthDate")),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata",
        });
      }
      const profile = await questApi.login(username, password);
      onReady(profile);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Please try again");
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="app-shell onboarding">
      <div className="onboard-stars">✦<span>✦</span><i>✦</i></div>
      <div className="brand"><span className="brand-mark">✦</span>Detour</div>
      <div className="auth-intro">
        <p className="eyebrow">YOUR CITY, REMIXED</p>
        <h1>{register ? <>Begin your <i>detour.</i></> : <>Welcome <i>back.</i></>}</h1>
        <p>Small reasons to take a different route today.</p>
      </div>
      <form action={submit} className="auth-form">
        <label>Username<input name="username" required minLength={3} placeholder="pick a quest name" /></label>
        {register && (
          <>
            <label>Email<input name="email" required type="email" placeholder="you@example.com" /></label>
            <label>Birth date<input name="birthDate" required type="date" /></label>
          </>
        )}
        <label>Password<input name="password" required minLength={register ? 10 : 1} type="password" placeholder="••••••••••" /></label>
        {error && <p className="form-error">{error}</p>}
        <button className="complete-button" disabled={busy}>{busy ? "One moment…" : register ? "Create account" : "Log in"}</button>
      </form>
      <button className="auth-switch" onClick={() => setRegister(!register)}>
        {register ? "Already playing? Log in" : "New here? Create an account"}
      </button>
      <p className="legal">By continuing, you confirm you are 18 or older.</p>
    </main>
  );
}

function ChipToggle({
  selected,
  onToggle,
  children,
}: {
  selected: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className={`chip ${selected ? "selected" : ""}`} aria-pressed={selected} onClick={onToggle}>
      {children}
    </button>
  );
}

function Setup({ profile, onUpdate, initialStep = "home", preferenceOnly = false }: { profile: Profile; onUpdate: (profile: Profile) => void; initialStep?: SetupStep; preferenceOnly?: boolean }) {
  const [step, setStep] = useState<SetupStep>(initialStep);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [areaQuery, setAreaQuery] = useState("");
  const [areas, setAreas] = useState<AreaCandidate[]>([]);
  const [selectedArea, setSelectedArea] = useState<AreaCandidate | null>(null);
  const [homeSource, setHomeSource] = useState<"address" | "live_location">("address");
  const [searchingAreas, setSearchingAreas] = useState(false);

  const [customLike, setCustomLike] = useState("");
  const [budget, setBudget] = useState<Budget>(profile.budget || "free");
  const [socialComfort] = useState<SocialComfort>(profile.socialComfort || "solo_only");
  const [environment] = useState<EnvironmentPreference>(profile.environmentPreference || "either");
  const [accessibility, setAccessibility] = useState(profile.accessibilityNotes || "");
  const [interestPreferences, setInterestPreferences] = useState(profile.interestPreferences);
  const [customInterests, setCustomInterests] = useState(profile.customInterests);
  const [primaryIntent, setPrimaryIntent] = useState<QuestIntent>(profile.primaryIntent);
  const [secondaryIntents, setSecondaryIntents] = useState<QuestIntent[]>(profile.secondaryIntents);
  const [activityStyles, setActivityStyles] = useState<ActivityStyle[]>(profile.activityStyles);
  const [primaryTravelMode, setPrimaryTravelMode] = useState<TravelMode>(profile.primaryTravelMode);
  const [fallbackTravelModes, setFallbackTravelModes] = useState<TravelMode[]>(profile.fallbackTravelModes);
  const [totalTimeMinutes, setTotalTimeMinutes] = useState(profile.totalTimeMinutes || 30);
  const [maxOneWayTravelMinutes, setMaxOneWayTravelMinutes] = useState(profile.maxOneWayTravelMinutes || 20);
  const [maxOneWayDistanceMetres, setMaxOneWayDistanceMetres] = useState(profile.maxOneWayDistanceMetres || 5_000);
  const [structuredAccessibility, setStructuredAccessibility] = useState<AccessibilityPreferences>(profile.accessibility);

  const selectedCenter = selectedArea
    ? { latitude: selectedArea.latitude, longitude: selectedArea.longitude }
    : null;
  const selectPinnedArea = (coordinate: { latitude: number; longitude: number }) =>
    setSelectedArea({
      city: selectedArea?.city || "Selected area",
      name: "Pinned area",
      ...coordinate,
    });

  const verify = async () => {
    setBusy(true);
    try {
      await questApi.verifyEmail();
      onUpdate({ ...profile, emailVerified: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not verify email");
    } finally {
      setBusy(false);
    }
  };

  const searchAreas = async () => {
    const query = areaQuery.trim();
    if (query.length < 2) {
      setError("Enter at least two characters of your address");
      setAreas([]);
      return;
    }
    setSearchingAreas(true);
    setError("");
    try {
      const results = await questApi.searchAreas(query);
      setAreas(results);
      if (results.length === 1) setSelectedArea(results[0]);
      else if (!results.length) {
        setError("No matching address was found. Try a fuller address or use your live location.");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not find that area");
    } finally {
      setSearchingAreas(false);
    }
  };

  const useLiveLocation = () => {
    setError("");
    if (!navigator.geolocation) {
      setError("Live location is not supported by this browser");
      return;
    }
    setSearchingAreas(true);
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        setSelectedArea({
          city: "Current location",
          name: "My current location",
          latitude: coords.latitude,
          longitude: coords.longitude,
        });
        setHomeSource("live_location");
        setAreas([]);
        setSearchingAreas(false);
      },
      (locationError) => {
        setError(
          locationError.code === 1
            ? "Location access was not allowed. You can enter your address instead."
            : "We could not get your location. Please try again."
        );
        setSearchingAreas(false);
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
    );
  };

  const setAffinity = (interest: string, affinity: "love" | "okay" | "avoid") => setInterestPreferences((current) => ({ ...current, [interest]: affinity }));
  const toggleSecondaryIntent = (intent: QuestIntent) => setSecondaryIntents((current) => current.includes(intent) ? current.filter((item) => item !== intent) : [...current, intent]);
  const toggleActivityStyle = (style: ActivityStyle) => setActivityStyles((current) => current.includes(style) ? current.filter((item) => item !== style) : [...current, style]);
  const addFallback = (mode: TravelMode) => setFallbackTravelModes((current) => current.includes(mode) ? current.filter((item) => item !== mode) : [...current, mode].filter((item) => item !== primaryTravelMode));

  const finish = async () => {
    setBusy(true);
    setError("");
    try {
      if (!selectedArea && initialStep === "home") {
        throw new Error("Enter your address or use your live location first");
      }
      const updated = await questApi.savePreferences({
        interestPreferences,
        customInterests,
        primaryIntent,
        secondaryIntents,
        activityStyles,
        primaryTravelMode,
        fallbackTravelModes,
        totalTimeMinutes,
        maxOneWayTravelMinutes,
        maxOneWayDistanceMetres,
        accessibility: { ...structuredAccessibility, notes: accessibility.trim() || null },
        budget,
        socialComfort,
        environmentPreference: environment,
      });
      const homeZone = selectedArea
        ? await questApi.setHomeZone({
            city: selectedArea.city,
            address: selectedArea.name,
            source: homeSource,
            latitude: selectedArea.latitude,
            longitude: selectedArea.longitude,
          })
        : profile.homeZone;
      onUpdate({ ...updated, homeZone });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save your setup");
    } finally {
      setBusy(false);
    }
  };

  if (!profile.emailVerified) {
    return (
      <main className="app-shell setup-page">
        <div className="brand"><span className="brand-mark">✦</span>Detour</div>
        <div className="setup-art">✉</div>
        <p className="eyebrow">ONE QUICK CHECK</p>
        <h1>Verify your <i>email.</i></h1>
        <p className="setup-copy">
          We use it for account recovery and security notices. In local development, this action verifies the account directly.
        </p>
        {error && <p className="form-error">{error}</p>}
        <button className="complete-button" disabled={busy} onClick={verify}>
          {busy ? "Verifying…" : "Verify email"}
        </button>
      </main>
    );
  }

  const stepIndex = ["home", "interests", "style", "limits"].indexOf(step) + 1;

  return (
    <main className="app-shell setup-page">
      <div className="brand"><span className="brand-mark">✦</span>Detour</div>
      <p className="step-indicator">Step {stepIndex} of 4</p>

      {step === "home" && (
        <>
          <p className="eyebrow">YOUR STARTING POINT</p>
          <h1>Where is <i>home?</i></h1>
          <p className="setup-copy">
            Search for your address or use your current location. Daily quests always start from this saved point.
          </p>
          <div className="auth-form setup-form">
            <label>
              Home address
              <div className="area-search">
                <input
                  value={areaQuery}
                  onChange={(event) => {
                    setAreaQuery(event.target.value);
                    setSelectedArea(null);
                    setHomeSource("address");
                  }}
                  placeholder="Street, building, city, or full address"
                />
                <button type="button" onClick={searchAreas} disabled={searchingAreas}>
                  {searchingAreas ? "…" : "Find"}
                </button>
              </div>
            </label>
            <div className="home-divider"><span>or</span></div>
            <button className="location-button" type="button" onClick={useLiveLocation} disabled={searchingAreas}>
              ⌖ Use my live location
            </button>
            <small className="privacy-note">
              Your browser will ask permission first. We only request location when you tap this button.
            </small>
            <AreaPickerMap
              selected={selectedCenter}
              onSelect={(coordinate) => {
                selectPinnedArea(coordinate);
                setHomeSource("address");
              }}
            />
            {areas.length > 0 && (
              <div className="area-results" role="listbox" aria-label="Address results">
                {areas.map((area) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={
                      selectedArea?.name === area.name && selectedArea.latitude === area.latitude
                    }
                    className={
                      selectedArea?.name === area.name && selectedArea.latitude === area.latitude
                        ? "selected"
                        : ""
                    }
                    onClick={() => {
                      setSelectedArea(area);
                      setHomeSource("address");
                    }}
                    key={`${area.name}-${area.latitude}-${area.longitude}`}
                  >
                    <b>{area.name}</b>
                    <span>{area.city}</span>
                  </button>
                ))}
              </div>
            )}
            {selectedArea && (
              <p className="area-selected">
                ⌂ Home: {selectedArea.name}
                {selectedArea.city !== selectedArea.name ? `, ${selectedArea.city}` : ""}
              </p>
            )}
            {error && <p className="form-error">{error}</p>}
            <button
              className="complete-button"
              type="button"
              disabled={!selectedArea}
              onClick={() => {
                setError("");
                setStep("interests");
              }}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {step === "interests" && (
        <>
          <p className="eyebrow">YOUR INTERESTS</p>
          <h1>What do you <i>love?</i></h1>
          <p className="setup-copy">Mark subjects you love, are okay with, or want us to avoid. Avoided subjects are hard exclusions.</p>
          <div className="interest-affinities">
            {INTEREST_OPTIONS.map(([value, label]) => <div className="interest-affinity" key={value}><b>{label}</b><div>{(["love", "okay", "avoid"] as const).map((affinity) => <button key={affinity} type="button" className={interestPreferences[value] === affinity ? "selected" : ""} onClick={() => setAffinity(value, affinity)}>{affinity}</button>)}</div></div>)}
          </div>
          <div className="auth-form setup-form"><label>Add a custom interest<div className="area-search"><input value={customLike} onChange={(event) => setCustomLike(event.target.value)} placeholder="e.g. murals, bookshops" /><button type="button" onClick={() => { const value = customLike.trim(); if (value && !customInterests.includes(value)) setCustomInterests([...customInterests, value]); setCustomLike(""); }}>Add</button></div></label>{customInterests.length > 0 && <p className="area-selected">Custom: {customInterests.join(", ")}</p>}</div>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            {!preferenceOnly && <button type="button" className="ghost-button" onClick={() => setStep("home")}>Back</button>}
            <button
              type="button"
              className="complete-button"
              onClick={() => {
                setError("");
                if (Object.values(interestPreferences).every((value) => value === "avoid")) {
                  setError("Keep at least one interest available");
                  return;
                }
                setStep("style");
              }}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {step === "style" && (
        <>
          <p className="eyebrow">QUEST STYLE</p>
          <h1>What do you want to <i>feel?</i></h1>
          <p className="setup-copy">Choose one primary intent. Secondary intents and activity styles add variety without overriding it.</p>
          <div className="chip-grid">{INTENT_OPTIONS.map((option) => <ChipToggle key={option.value} selected={primaryIntent === option.value} onToggle={() => { setPrimaryIntent(option.value); setSecondaryIntents((current) => current.filter((item) => item !== option.value)); }}>{option.label}</ChipToggle>)}</div>
          <p className="chip-hint">Primary intent</p>
          <div className="chip-grid">{INTENT_OPTIONS.filter((option) => option.value !== primaryIntent).map((option) => <ChipToggle key={option.value} selected={secondaryIntents.includes(option.value)} onToggle={() => toggleSecondaryIntent(option.value)}>{option.label}</ChipToggle>)}</div>
          <p className="chip-hint">Optional secondary intents</p>
          <div className="chip-grid">{ACTIVITY_OPTIONS.map((option) => <ChipToggle key={option.value} selected={activityStyles.includes(option.value)} onToggle={() => toggleActivityStyle(option.value)}>{option.label}</ChipToggle>)}</div>
          <p className="chip-hint">Activity styles</p>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            <button type="button" className="ghost-button" onClick={() => setStep("interests")}>Back</button>
            <button type="button" className="complete-button" onClick={() => {
              if (!activityStyles.length) {
                setError("Select at least one activity style");
                return;
              }
              setError("");
              setStep("limits");
            }}>
              Continue
            </button>
          </div>
        </>
      )}

      {step === "limits" && (
        <>
          <p className="eyebrow">PRACTICAL LIMITS</p>
          <h1>How will you <i>get there?</i></h1>
          <p className="setup-copy">Your main mode decides reachability. Fallbacks are only used when it cannot find enough good matches.</p>
          <div className="auth-form setup-form">
            <fieldset><legend>Primary transport</legend><div className="chip-grid">{TRAVEL_MODE_OPTIONS.map((option) => <ChipToggle key={option.value} selected={primaryTravelMode === option.value} onToggle={() => { setPrimaryTravelMode(option.value); setFallbackTravelModes((current) => current.filter((mode) => mode !== option.value)); }}>{option.icon} {option.label}</ChipToggle>)}</div></fieldset>
            <fieldset><legend>Fallback transport, in order</legend><div className="chip-grid">{TRAVEL_MODE_OPTIONS.filter((option) => option.value !== primaryTravelMode).map((option) => <ChipToggle key={option.value} selected={fallbackTravelModes.includes(option.value)} onToggle={() => addFallback(option.value)}>{fallbackTravelModes.includes(option.value) ? `${fallbackTravelModes.indexOf(option.value) + 1}. ` : ""}{option.icon} {option.label}</ChipToggle>)}</div></fieldset>
            <label>
              Time you usually have
              <select
                value={totalTimeMinutes}
                onChange={(event) => setTotalTimeMinutes(Number(event.target.value))}
              >
                <option value={15}>15 minutes</option>
                <option value={30}>30 minutes</option>
                <option value={60}>60 minutes</option>
                <option value={90}>90 minutes</option>
              </select>
            </label>
            <label>
              Max travel time one way
              <select
                value={maxOneWayTravelMinutes}
                onChange={(event) => setMaxOneWayTravelMinutes(Number(event.target.value))}
              >
                <option value={10}>10 minutes</option>
                <option value={20}>20 minutes</option>
                <option value={40}>40 minutes</option>
                <option value={60}>60 minutes</option>
                <option value={90}>90 minutes</option>
                <option value={120}>2 hours</option>
              </select>
            </label>
            <label>
              Max distance one way: {maxOneWayDistanceMetres / 1_000} km
              <input
                className="distance-slider"
                type="range"
                min={1_000}
                max={150_000}
                step={1_000}
                value={maxOneWayDistanceMetres}
                onChange={(event) => setMaxOneWayDistanceMetres(Number(event.target.value))}
                aria-valuetext={`${maxOneWayDistanceMetres / 1_000} km`}
              />
              <small>1 km <span>150 km</span></small>
            </label>
            <label>
              Budget
              <select value={budget} onChange={(event) => setBudget(event.target.value as Budget)}>
                <option value="free">Free only</option>
                <option value="low">Free or low cost</option>
              </select>
            </label>
            <label>
              Accessibility notes
              <input
                value={accessibility}
                onChange={(event) => setAccessibility(event.target.value)}
                placeholder="Optional — e.g. wheelchair access preferred"
              />
            </label>
            <fieldset><legend>Accessibility needs</legend>{([ ["stepFree", "Step-free access"], ["wheelchairAccess", "Wheelchair access"], ["seating", "Seating available"], ["lowSensory", "Low-sensory places"] ] as const).map(([key, label]) => <label className="check" key={key}><input type="checkbox" checked={structuredAccessibility[key]} onChange={() => setStructuredAccessibility((current) => ({ ...current, [key]: !current[key] }))} />{label}</label>)}<label>Maximum walking time<select value={structuredAccessibility.maxWalkingMinutes ?? ""} onChange={(event) => setStructuredAccessibility((current) => ({ ...current, maxWalkingMinutes: event.target.value ? Number(event.target.value) : null }))}><option value="">No limit</option><option value={10}>10 minutes</option><option value={20}>20 minutes</option><option value={40}>40 minutes</option></select></label></fieldset>
          </div>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            <button type="button" className="ghost-button" onClick={() => setStep("style")}>Back</button>
            <button className="complete-button" type="button" disabled={busy} onClick={finish}>
              {busy ? "Saving…" : preferenceOnly ? "Save preferences" : "Save & show quests"}
            </button>
          </div>
        </>
      )}
    </main>
  );
}
