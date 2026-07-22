"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  INTEREST_CHIPS,
  authDisabled,
  questApi,
} from "../lib/quest-api";
import AreaPickerMap from "../components/AreaPickerMap";
import QuestMap from "../components/QuestMap";
import type {
  AreaCandidate,
  Budget,
  EnvironmentPreference,
  Motivation,
  MovementIntensity,
  Profile,
  Progress,
  Quest,
  SocialComfort,
} from "../lib/quest-api";

type Tab = "map" | "quests" | "me";
type SetupStep = "home" | "motivations" | "interests" | "limits";

const QUEST_TYPE_OPTIONS = [
  { value: "nature", label: "Nature", detail: "Parks, gardens, and open air" },
  { value: "culture", label: "Culture", detail: "History, art, and local stories" },
  { value: "creativity", label: "Create", detail: "Make, notice, and imagine" },
  { value: "mindfulness", label: "Reset", detail: "Quiet and reflective moments" },
  { value: "fitness", label: "Move", detail: "Gentle ways to get moving" },
  { value: "learning", label: "Learn", detail: "Books, exhibits, and discovery" },
] as const;
type QuestType = (typeof QUEST_TYPE_OPTIONS)[number]["value"];

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
  maxWalkingMinutes: 20,
  movementIntensity: "gentle",
  budget: "free",
  socialComfort: "solo_only",
  environmentPreference: "either",
  accessibilityNotes: null,
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
  const [profile, setProfile] = useState<Profile | null>(null);
  const [booted, setBooted] = useState(false);
  const [editingPreferences, setEditingPreferences] = useState(false);

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
  const sync = async () => { const deck = await questApi.today(); setQuests([...deck.quests]); setRefreshAvailable(deck.refreshAvailable); };
  const complete = async (quest: Quest) => {
    if (pending || quest.status !== "offered") return;
    setPending(quest.id); setToast("Completion pending sync");
    const result = await questApi.complete(quest.id);
    setProgress(result.progress); await sync(); setPending(null); setActive(result.quest || null); setToast(`+${quest.xp} XP earned`); window.setTimeout(() => setToast(null), 2200);
  };
  const skip = async (quest: Quest) => { await questApi.skip(quest.id); await sync(); setActive(null); };
  const refresh = async () => { const deck = await questApi.refreshDeck(); setQuests([...deck.quests]); setRefreshAvailable(deck.refreshAvailable); setToast("Your city deck has been refreshed"); window.setTimeout(() => setToast(null), 2200); };
  const generate = async () => {
    if (generating) return;
    setGenerating(true);
    setToast("Finding quests near you…");
    try {
      const deck = await questApi.today();
      setQuests([...deck.quests]);
      setRefreshAvailable(deck.refreshAvailable);
      setToast(deck.quests.length ? "Your quests are ready" : "No quests were generated. Try again.");
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Could not generate quests. Try again.");
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
        initialStep="motivations"
        onUpdate={(updatedProfile) => {
          setProfile(updatedProfile);
          setEditingPreferences(false);
        }}
      />
    );
  }

  return <main className={`app-shell ${tab === "map" ? "map-app" : ""}`}>
    <header className="topbar"><div className="brand"><span className="brand-mark">✦</span><span>Detour</span></div><button className="avatar" onClick={() => setTab("me")}>S</button></header>
    {tab === "map" && <QuestMap quests={quests} activeQuest={active} onSelectQuest={setActive} homeCenter={profile.homeZone.center} homeLabel={profile.homeZone.city} level={progress.level} xp={progress.xp} completedCount={completed} dateLabel={new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: profile.timezone }).format(new Date()).toUpperCase()} refreshAvailable={refreshAvailable} onRefresh={refresh} onGenerate={generate} generating={generating} />}
    {tab === "quests" && <QuestList quests={quests} onSelect={setActive} onGenerate={generate} generating={generating} />}
    {tab === "me" && <UserProfile progress={progress} completed={completed} onEditPreferences={() => setEditingPreferences(true)} onSignOut={authDisabled ? undefined : async () => { await questApi.logout(); setProfile(null); }} />}
    <nav className="bottom-nav"><button className={tab === "map" ? "selected" : ""} onClick={() => setTab("map")}><span>⌖</span>Map</button><button className={tab === "quests" ? "selected" : ""} onClick={() => setTab("quests")}><span>☷</span>Quests</button><button className={tab === "me" ? "selected" : ""} onClick={() => setTab("me")}><span>◉</span>Me</button></nav>
    {active && <QuestSheet quest={active} pending={pending === active.id} onClose={() => setActive(null)} onComplete={() => complete(active)} onSkip={() => skip(active)} />}
    {toast && <div className="toast">{toast}</div>}
  </main>;
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
              <small>{q.category} · {q.distance}</small>
              <b>{q.title}</b>
              <em>{q.status === "completed" ? "Completed" : q.status === "skipped" ? "Unavailable" : `+${q.xp} XP`}</em>
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

function QuestSheet({ quest, pending, onClose, onComplete, onSkip }: { quest: Quest; pending: boolean; onClose: () => void; onComplete: () => void; onSkip: () => void }) {
  const done = quest.status === "completed";
  const walkLabel =
    quest.walkingMinutes != null
      ? quest.distanceSource === "approximate"
        ? `~${quest.walkingMinutes} min walk`
        : `${quest.walkingMinutes} min walk`
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
        <div className="quest-meta">
          {walkLabel && <span>🚶 {walkLabel}</span>}
          {activityLabel && <span>◷ {activityLabel}</span>}
          {!walkLabel && !activityLabel && <span>◷ {quest.duration}</span>}
          <span>◒ {quest.time}</span>
          <b>+{quest.xp} XP</b>
        </div>
        {done ? (
          <button className="completed-button" disabled>✓ Quest completed</button>
        ) : quest.status === "skipped" ? (
          <button className="skipped-button" disabled>Quest unavailable</button>
        ) : (
          <>
            <button className="complete-button" disabled={pending} onClick={onComplete}>
              {pending ? "Syncing completion…" : "✓  Completed"}
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
    <button type="button" className={`chip ${selected ? "selected" : ""}`} onClick={onToggle}>
      {children}
    </button>
  );
}

function Setup({ profile, onUpdate, initialStep = "home" }: { profile: Profile; onUpdate: (profile: Profile) => void; initialStep?: SetupStep }) {
  const [step, setStep] = useState<SetupStep>(initialStep);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [areaQuery, setAreaQuery] = useState("");
  const [areas, setAreas] = useState<AreaCandidate[]>([]);
  const [selectedArea, setSelectedArea] = useState<AreaCandidate | null>(null);
  const [homeSource, setHomeSource] = useState<"address" | "live_location">("address");
  const [searchingAreas, setSearchingAreas] = useState(false);

  const motivations: Motivation[] = profile.motivations.length
    ? profile.motivations
    : ["explore"];
  const [questTypes, setQuestTypes] = useState<QuestType[]>(
    profile.categories.filter((category): category is QuestType =>
      QUEST_TYPE_OPTIONS.some((option) => option.value === category)
    ).slice(0, 4)
  );
  const [likes, setLikes] = useState<string[]>(profile.likes);
  const [customLike, setCustomLike] = useState("");
  const [dislikes, setDislikes] = useState(profile.dislikes.join(", "));
  const [socialComfort, setSocialComfort] = useState<SocialComfort>(profile.socialComfort || "solo_only");
  const [environment, setEnvironment] = useState<EnvironmentPreference>(
    profile.environmentPreference || "either"
  );
  const [availableMinutes, setAvailableMinutes] = useState(profile.availableMinutes || 30);
  const [maxWalking, setMaxWalking] = useState(profile.maxWalkingMinutes || 20);
  const [intensity, setIntensity] = useState<MovementIntensity>(profile.movementIntensity || "gentle");
  const [budget, setBudget] = useState<Budget>(profile.budget || "free");
  const [accessibility, setAccessibility] = useState(profile.accessibilityNotes || "");

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

  const toggleQuestType = (value: QuestType) => {
    setQuestTypes((current) => {
      if (current.includes(value)) {
        return current.filter((item) => item !== value);
      }
      if (current.length >= 4) return current;
      return [...current, value];
    });
  };

  const toggleLike = (value: string) => {
    setLikes((current) =>
      current.includes(value) ? current.filter((item) => item !== value) : [...current, value].slice(0, 20)
    );
  };

  const addCustomLike = () => {
    const value = customLike.trim();
    if (!value) return;
    setLikes((current) => (current.includes(value) ? current : [...current, value].slice(0, 20)));
    setCustomLike("");
  };

  const finish = async () => {
    setBusy(true);
    setError("");
    try {
      if (!selectedArea && initialStep === "home") {
        throw new Error("Enter your address or use your live location first");
      }
      if (questTypes.length < 1 || questTypes.length > 4) {
        throw new Error("Pick between one and four quest types");
      }
      const categories = questTypes;
      const dislikeList = dislikes
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean)
        .slice(0, 20);
      await questApi.savePreferences({
        likes,
        dislikes: dislikeList,
        categories,
        motivations,
        availableMinutes,
        maxWalkingMinutes: maxWalking,
        movementIntensity: intensity,
        budget,
        socialComfort,
        environmentPreference: environment,
        accessibilityNotes: accessibility.trim() || null,
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
      onUpdate({
        ...profile,
        likes,
        dislikes: dislikeList,
        categories,
        motivations,
        availableMinutes,
        maxWalkingMinutes: maxWalking,
        movementIntensity: intensity,
        budget,
        socialComfort,
        environmentPreference: environment,
        accessibilityNotes: accessibility.trim() || null,
        homeZone,
      });
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

  const stepIndex = ["home", "motivations", "interests", "limits"].indexOf(step) + 1;

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
                setStep("motivations");
              }}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {step === "motivations" && (
        <>
          <p className="eyebrow">YOUR QUEST MENU</p>
          <h1>What sounds <i>good?</i></h1>
          <p className="setup-copy">Choose 1–4 quest types. We will use these to decide what to look for near you.</p>
          <div className="chip-grid">
            {QUEST_TYPE_OPTIONS.map((option) => (
              <ChipToggle
                key={option.value}
                selected={questTypes.includes(option.value)}
                onToggle={() => toggleQuestType(option.value)}
              >
                {option.label}
              </ChipToggle>
            ))}
          </div>
          <p className="chip-hint">{questTypes.length}/4 selected</p>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            <button type="button" className="ghost-button" onClick={() => setStep("home")}>Back</button>
            <button
              type="button"
              className="complete-button"
              onClick={() => {
                if (questTypes.length < 1) {
                  setError("Pick at least one quest type");
                  return;
                }
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
          <p className="eyebrow">CURIOSITY & BOUNDARIES</p>
          <h1>What are you <i>curious</i> about?</h1>
          <p className="setup-copy">Tap chips or add your own. Dislikes stay as hard exclusions.</p>
          <div className="chip-grid">
            {INTEREST_CHIPS.map((chip) => (
              <ChipToggle key={chip} selected={likes.includes(chip)} onToggle={() => toggleLike(chip)}>
                {chip}
              </ChipToggle>
            ))}
          </div>
          <div className="auth-form setup-form">
            <label>
              Add something else
              <div className="area-search">
                <input
                  value={customLike}
                  onChange={(event) => setCustomLike(event.target.value)}
                  placeholder="e.g. murals, bookshops"
                />
                <button type="button" onClick={addCustomLike}>Add</button>
              </div>
            </label>
            {likes.length > 0 && (
              <p className="area-selected">Curious about: {likes.join(", ")}</p>
            )}
            <label>
              Soft exclusions
              <input
                value={dislikes}
                onChange={(event) => setDislikes(event.target.value)}
                placeholder="shopping, loud crowds (comma-separated)"
              />
            </label>
            <fieldset>
              <legend>Social comfort</legend>
              <label className="check">
                <input
                  type="radio"
                  name="social"
                  checked={socialComfort === "solo_only"}
                  onChange={() => setSocialComfort("solo_only")}
                />
                Solo only
              </label>
              <label className="check">
                <input
                  type="radio"
                  name="social"
                  checked={socialComfort === "optional_interaction"}
                  onChange={() => setSocialComfort("optional_interaction")}
                />
                Optional interaction
              </label>
            </fieldset>
            <fieldset>
              <legend>Environment</legend>
              {(["either", "outdoor", "indoor"] as EnvironmentPreference[]).map((value) => (
                <label className="check" key={value}>
                  <input
                    type="radio"
                    name="environment"
                    checked={environment === value}
                    onChange={() => setEnvironment(value)}
                  />
                  {value}
                </label>
              ))}
            </fieldset>
          </div>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            <button type="button" className="ghost-button" onClick={() => setStep("motivations")}>Back</button>
            <button type="button" className="complete-button" onClick={() => { setError(""); setStep("limits"); }}>
              Continue
            </button>
          </div>
        </>
      )}

      {step === "limits" && (
        <>
          <p className="eyebrow">PRACTICAL LIMITS</p>
          <h1>How far will you <i>go?</i></h1>
          <p className="setup-copy">These hard limits keep every quest walkable and within your day.</p>
          <div className="auth-form setup-form">
            <label>
              Time you usually have
              <select
                value={availableMinutes}
                onChange={(event) => setAvailableMinutes(Number(event.target.value))}
              >
                <option value={15}>15 minutes</option>
                <option value={30}>30 minutes</option>
                <option value={60}>60 minutes</option>
                <option value={90}>90 minutes</option>
              </select>
            </label>
            <label>
              Max walking one way
              <select
                value={maxWalking}
                onChange={(event) => setMaxWalking(Number(event.target.value))}
              >
                <option value={10}>10 minutes</option>
                <option value={20}>20 minutes</option>
                <option value={40}>40 minutes</option>
                <option value={60}>60 minutes</option>
              </select>
            </label>
            <label>
              Movement intensity
              <select
                value={intensity}
                onChange={(event) => setIntensity(event.target.value as MovementIntensity)}
              >
                <option value="gentle">Gentle</option>
                <option value="moderate">Moderate</option>
                <option value="energetic">Energetic</option>
              </select>
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
          </div>
          {error && <p className="form-error">{error}</p>}
          <div className="setup-nav">
            <button type="button" className="ghost-button" onClick={() => setStep("interests")}>Back</button>
            <button className="complete-button" type="button" disabled={busy} onClick={finish}>
              {busy ? "Saving…" : "Save & show quests"}
            </button>
          </div>
        </>
      )}
    </main>
  );
}
