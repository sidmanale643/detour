"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { questApi } from "../lib/quest-api";
import AreaPickerMap from "../components/AreaPickerMap";
import QuestMap from "../components/QuestMap";
import type {
  AreaCandidate,
  Discovery,
  DiscoveryPlace,
  Profile,
  Progress,
  Quest,
} from "../lib/quest-api";

type Tab = "map" | "discover" | "quests" | "me";
type SetupStep = "home" | "interests" | "limits";

const INTEREST_OPTIONS = [
  ["explorer", "Explorer"], ["foodie", "Foodie"],
  ["skill_builder", "Skill Builder"], ["social_connector", "Social Connector"],
  ["adventurer", "Adventurer"], ["nature_mindfulness", "Nature & Mindfulness"],
] as const;
const INTEREST_LABELS = Object.fromEntries(INTEREST_OPTIONS) as Record<string, string>;


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

  const loadDiscovery = useCallback(async () => {
    setDiscovering(true);
    setDiscoverError(null);
    try {
      setDiscovery(await questApi.discover());
    } catch (error) {
      setDiscoverError(error instanceof Error ? error.message : "Could not discover places right now.");
    } finally {
      setDiscovering(false);
    }
  }, []);

  useEffect(() => {
    questApi.profile()
      .then(setProfile)
      .catch((error) => setQuestError(error instanceof Error ? error.message : "Could not load your local profile."))
      .finally(() => setBooted(true));
  }, []);
  const completed = useMemo(() => quests.filter((q) => q.status === "completed").length, [quests]);
  const sync = useCallback(async () => {
    const deck = await questApi.today();
    setQuests([...deck.quests]);
    setRefreshAvailable(deck.refreshAvailable);
  }, []);
  // Load progress + today's deck whenever the player has a home zone (including boot).
  // Without this, generated quests stay in SQLite but the UI starts empty after refresh.
  useEffect(() => {
    if (!profile?.homeZone) return;
    questApi.progressSummary().then(setProgress).catch(() => undefined);
    void sync().catch((error) => {
      setQuestError(
        error instanceof Error
          ? error.message
          : "Could not load today’s quests."
      );
    });
  }, [profile, sync]);
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
  if (!profile) return <main className="app-shell splash"><span className="brand-mark">!</span><b>{questError || "Could not load Detour."}</b></main>;
  if (!profile.homeZone) return <Setup profile={profile} onUpdate={setProfile} />;
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
    {tab === "map" && <QuestMap quests={quests} activeQuest={active ?? quests.find((quest) => quest.status === "active") ?? null} onSelectQuest={setActive} homeCenter={profile.homeZone.center} homeLabel={profile.homeZone.city} level={progress.level} xp={progress.xp} completedCount={completed} dateLabel={new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: profile.timezone }).format(new Date()).toUpperCase()} refreshAvailable={refreshAvailable} onRefresh={refresh} onGenerate={generate} generating={generating} />}
    {tab === "discover" && <DiscoverPage city={profile.homeZone.city} discovery={discovery} loading={discovering} error={discoverError} onLoad={loadDiscovery} />}
    {tab === "quests" && <QuestList quests={quests} onSelect={setActive} onGenerate={generate} generating={generating} />}
    {tab === "me" && <UserProfile progress={progress} completed={completed} onEditPreferences={() => setEditingPreferences(true)} />}
    <nav className="bottom-nav"><button className={tab === "map" ? "selected" : ""} onClick={() => setTab("map")}><span>⌖</span>Map</button><button className={tab === "discover" ? "selected" : ""} onClick={() => { setTab("discover"); if (!discovery && !discovering) void loadDiscovery(); }}><span>✦</span>Discover</button><button className={tab === "quests" ? "selected" : ""} onClick={() => setTab("quests")}><span>☷</span>Quests</button><button className={tab === "me" ? "selected" : ""} onClick={() => setTab("me")}><span>◉</span>Me</button></nav>
    {active && <QuestSheet quest={active} pending={pending === active.id} onClose={() => setActive(null)} onStart={() => start(active)} onComplete={() => complete(active)} onSkip={() => skip(active)} onExpired={() => expireActiveQuest(active.id)} />}
    {toast && <div className="toast">{toast}</div>}
  </main>;
}

function DiscoverPage({ city, discovery, loading, error, onLoad }: { city: string; discovery: Discovery | null; loading: boolean; error: string | null; onLoad: () => Promise<void> }) {
  return <section className="discover-page">
    <p className="eyebrow">LIVE CITY GUIDE</p>
    <h1>find your next<br /><i>side quest.</i></h1>
    <p className="discover-copy">Places that match your selected interests within your saved distance in {discovery?.city || city}.</p>
    {error && <div className="discover-error" role="alert">{error}<button type="button" onClick={() => void onLoad()}>Try again</button></div>}
    {loading && !discovery && <p className="discover-status">Finding real places…</p>}
    {discovery && <>
      <DiscoverSection title="Your matches" subtitle="Selected interests, searched from your home coordinate" places={discovery.matches} empty="No public places matching your preferences were found within your selected distance." />
    </>}
  </section>;
}

function DiscoverSection({ title, subtitle, places, empty }: { title: string; subtitle: string; places: DiscoveryPlace[]; empty: string }) {
  return <section className="discover-section"><div><h2>{title}</h2><p>{subtitle}</p></div>{places.length ? <div className="discover-cards">{places.map((place) => <DiscoverCard key={`${place.provider}:${place.providerId}`} place={place} />)}</div> : <p className="discover-empty">{empty}</p>}</section>;
}

function DiscoverCard({ place }: { place: DiscoveryPlace }) {
  const distance = place.distanceMetres < 1000 ? `${Math.round(place.distanceMetres / 10) * 10} m` : `${(place.distanceMetres / 1000).toFixed(1)} km`;
  const category = INTEREST_LABELS[place.matchingInterest] || place.matchingInterest;
  const content = <><span className="discover-kind">{place.placeType}</span><b>{place.name}</b>{place.description && <em>{place.description}</em>}<small>{distance} from home · {category} · Source: OpenStreetMap</small></>;
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
              <small>{q.topic || q.category} · {q.distance}</small>
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

function UserProfile({ progress, completed, onEditPreferences }: { progress: Progress; completed: number; onEditPreferences: () => void }) {
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
        {quest.topic && <p className="quest-metadata">{quest.topic}</p>}
        {quest.matchReasons.length > 0 && <div className="match-reasons">{quest.matchReasons.map((reason) => <span key={reason}>{reason}</span>)}</div>}
        <div className="quest-meta">
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
  const [interestPreferences, setInterestPreferences] = useState(profile.interestPreferences);
  const [customInterests, setCustomInterests] = useState(profile.customInterests);
  const [maxOneWayDistanceMetres, setMaxOneWayDistanceMetres] = useState(profile.maxOneWayDistanceMetres || 5_000);

  const selectedCenter = selectedArea
    ? { latitude: selectedArea.latitude, longitude: selectedArea.longitude }
    : null;
  const selectPinnedArea = (coordinate: { latitude: number; longitude: number }) =>
    setSelectedArea({
      city: selectedArea?.city || "Selected area",
      name: "Pinned area",
      ...coordinate,
    });

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
        maxOneWayDistanceMetres,
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
          <div className="setup-fields setup-form">
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
          <div className="setup-fields setup-form"><label>Add a custom interest<div className="area-search"><input value={customLike} onChange={(event) => { const value = event.target.value; setCustomLike(value); }} placeholder="e.g. murals, bookshops" /><button type="button" onClick={() => { const value = customLike.trim(); if (value && !customInterests.includes(value)) setCustomInterests([...customInterests, value]); setCustomLike(""); }}>Add</button></div></label>{customInterests.length > 0 && <p className="area-selected">Custom: {customInterests.join(", ")}</p>}</div>
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
                setStep("limits");
              }}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {step === "limits" && (
        <>
          <p className="eyebrow">PRACTICAL LIMITS</p>
          <h1>How far will you <i>go?</i></h1>
          <p className="setup-copy">Choose the maximum straight-line distance for a quest destination.</p>
          <div className="setup-fields setup-form">
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
          </div>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            <button type="button" className="ghost-button" onClick={() => setStep("interests")}>Back</button>
            <button className="complete-button" type="button" disabled={busy} onClick={finish}>
              {busy ? "Saving…" : preferenceOnly ? "Save preferences" : "Save & show quests"}
            </button>
          </div>
        </>
      )}
    </main>
  );
}
