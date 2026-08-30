"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type RestoryEvent = {
  id: number;
  timestamp: string;
  tool_name: string;
  tags: string[];
  danger: boolean;
  reason: string;
  command: string;
  event: string;
  decision: string;
  // `severity` may not exist on this branch's backend — treat as optional.
  severity?: string;
};

function fmtTime(ts: string): string {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

type Stats = {
  total: number;
  blocked: number;
  approved: number;
  blockedPct: number;
  tags: { name: string; count: number }[];
  tagMax: number;
  severities: { name: string; count: number }[] | null;
  sevMax: number;
  spark: number[];
  sparkPeak: number;
};

// Fixed order + palette for known severity levels; anything else falls through.
const SEV_ORDER = ["critical", "block", "warn", "info"];

function computeStats(events: RestoryEvent[]): Stats {
  const total = events.length;
  const blocked = events.filter((e) => e.danger).length;
  const approved = events.filter((e) => e.decision === "approve").length;
  const blockedPct = total > 0 ? Math.round((blocked / total) * 100) : 0;

  // --- breakdown by tag (danger class) ---
  const tagCounts = new Map<string, number>();
  for (const e of events) {
    for (const t of e.tags ?? []) {
      tagCounts.set(t, (tagCounts.get(t) ?? 0) + 1);
    }
  }
  const tags = Array.from(tagCounts, ([name, count]) => ({ name, count })).sort(
    (a, b) => b.count - a.count || a.name.localeCompare(b.name),
  );
  const tagMax = tags.reduce((m, t) => Math.max(m, t.count), 0);

  // --- breakdown by severity, only if the field is actually present ---
  const hasSeverity = events.some(
    (e) => typeof e.severity === "string" && e.severity.length > 0,
  );
  let severities: { name: string; count: number }[] | null = null;
  let sevMax = 0;
  if (hasSeverity) {
    const sevCounts = new Map<string, number>();
    for (const e of events) {
      const s = (e.severity ?? "").toLowerCase();
      if (!s) continue;
      sevCounts.set(s, (sevCounts.get(s) ?? 0) + 1);
    }
    severities = Array.from(sevCounts, ([name, count]) => ({ name, count })).sort(
      (a, b) => {
        const ia = SEV_ORDER.indexOf(a.name);
        const ib = SEV_ORDER.indexOf(b.name);
        if (ia !== ib) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
        return b.count - a.count;
      },
    );
    sevMax = severities.reduce((m, s) => Math.max(m, s.count), 0);
  }

  // --- activity over time: events per minute across the observed window ---
  const spark = buildPerMinute(events);
  const sparkPeak = spark.reduce((m, v) => Math.max(m, v), 0);

  return {
    total,
    blocked,
    approved,
    blockedPct,
    tags,
    tagMax,
    severities,
    sevMax,
    spark,
    sparkPeak,
  };
}

// Bucket events into per-minute counts spanning first→last, capped so the
// sparkline stays compact. Returns [] when there isn't a usable time span.
function buildPerMinute(events: RestoryEvent[]): number[] {
  const mins: number[] = [];
  for (const e of events) {
    const t = new Date(e.timestamp).getTime();
    if (!isNaN(t)) mins.push(Math.floor(t / 60000));
  }
  if (mins.length < 2) return [];
  const lo = Math.min(...mins);
  const hi = Math.max(...mins);
  const span = hi - lo + 1;
  if (span < 2) return [];
  const MAX_BUCKETS = 30;
  // If the window is huge, widen each bucket so we never draw > MAX_BUCKETS.
  const step = Math.ceil(span / MAX_BUCKETS);
  const buckets = Math.ceil(span / step);
  const out = new Array(buckets).fill(0);
  for (const m of mins) {
    const idx = Math.floor((m - lo) / step);
    out[idx] += 1;
  }
  return out;
}

function Sparkline({ data, peak }: { data: number[]; peak: number }) {
  const W = 260;
  const H = 40;
  if (data.length < 2 || peak <= 0) return null;
  const step = W / (data.length - 1);
  const pts = data.map((v, i) => {
    const x = i * step;
    const y = H - (v / peak) * (H - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const area = `0,${H} ${pts.join(" ")} ${W},${H}`;
  return (
    <svg
      className="spark-svg"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="events per minute"
    >
      <polygon className="spark-area" points={area} />
      <polyline className="spark-line" points={pts.join(" ")} />
    </svg>
  );
}

function StatBar({
  name,
  count,
  max,
  danger,
}: {
  name: string;
  count: number;
  max: number;
  danger?: boolean;
}) {
  const pct = max > 0 ? Math.round((count / max) * 100) : 0;
  return (
    <div className="statbar">
      <span className="statbar-label">{name}</span>
      <div className="statbar-track">
        <div
          className={`statbar-fill${danger ? " danger" : ""}`}
          style={{ width: `${Math.max(pct, 3)}%` }}
        />
      </div>
      <span className="statbar-count">{count}</span>
    </div>
  );
}

function StatsPanel({ stats }: { stats: Stats }) {
  return (
    <div className="stats">
      <div className="stats-grid">
        <div className="stat-tile">
          <span className="stat-num">{stats.total}</span>
          <span className="stat-cap">events</span>
        </div>
        <div className="stat-tile">
          <span className="stat-num danger">{stats.blocked}</span>
          <span className="stat-cap">blocked</span>
        </div>
        <div className="stat-tile">
          <span className="stat-num safe">{stats.approved}</span>
          <span className="stat-cap">approved</span>
        </div>
        <div className="stat-tile">
          <span className="stat-num">{stats.blockedPct}%</span>
          <span className="stat-cap">blocked rate</span>
        </div>
      </div>

      <div className="stats-section">
        <div className="stats-head">by danger class</div>
        {stats.tags.length > 0 ? (
          <div className="statbars">
            {stats.tags.map((t) => (
              <StatBar key={t.name} name={t.name} count={t.count} max={stats.tagMax} danger />
            ))}
          </div>
        ) : (
          <div className="stats-none">no danger tags yet</div>
        )}
      </div>

      {stats.severities && (
        <div className="stats-section">
          <div className="stats-head">by severity</div>
          <div className="statbars">
            {stats.severities.map((s) => (
              <StatBar key={s.name} name={s.name} count={s.count} max={stats.sevMax} />
            ))}
          </div>
        </div>
      )}

      {stats.spark.length >= 2 && (
        <div className="stats-section">
          <div className="stats-head">
            activity <span className="stats-sub">events / min · peak {stats.sparkPeak}</span>
          </div>
          <Sparkline data={stats.spark} peak={stats.sparkPeak} />
        </div>
      )}
    </div>
  );
}

function EventCard({ ev }: { ev: RestoryEvent }) {
  const isFileOp = ["Write", "Edit", "MultiEdit"].includes(ev.tool_name);
  const sigil = isFileOp ? "±" : "$";
  return (
    <div className="event">
      <span className={`node${ev.danger ? " danger" : ""}`} />
      <div className={`card${ev.danger ? " danger" : ""}`}>
        {ev.danger && (
          <div className="blocked-banner">
            <span>⛔ Blocked</span>
            <span className="reason">{ev.reason}</span>
          </div>
        )}
        <div className="card-body">
          <div className="row1">
            <span className="tool">{ev.tool_name || "tool"}</span>
            {ev.event && <span className="evt-kind">{ev.event}</span>}
            <span className="time">{fmtTime(ev.timestamp)}</span>
          </div>
          <code className={`cmd${ev.danger ? " danger" : ""}`}>
            <span className={`sigil${ev.danger ? "" : " safe"}`}>{sigil}</span>
            {ev.command || <span style={{ color: "var(--faint)" }}>(no command)</span>}
          </code>
          <div className="badges">
            {ev.tags.length > 0 ? (
              ev.tags.map((t) => (
                <span key={t} className="badge danger">
                  {t}
                </span>
              ))
            ) : (
              <span className="badge">safe</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Home() {
  const [events, setEvents] = useState<RestoryEvent[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);
  const [showStats, setShowStats] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/events", { cache: "no-store" });
      const data = await res.json();
      setEvents(data.events ?? []);
    } catch {
      /* server not ready yet; keep last state */
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 1000);
    return () => clearInterval(id);
  }, [load]);

  const flash = (msg: string) => {
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  };

  const undo = async () => {
    setUndoing(true);
    try {
      const res = await fetch("/api/undo", { method: "POST" });
      const data = await res.json();
      flash(
        data.ok
          ? `${data.message} ${(data.reverted ?? [])
              .map((r: { path: string }) => r.path)
              .join(", ")}`.trim()
          : data.message ?? "Undo failed.",
      );
      await load();
    } catch {
      flash("Undo request failed.");
    } finally {
      setUndoing(false);
    }
  };

  const blocked = events.filter((e) => e.danger).length;
  const stats = useMemo(() => computeStats(events), [events]);

  return (
    <main className="shell">
      <div className="topbar">
        <div className="brand">
          <b>restory</b>
          <span>blast-radius monitor</span>
        </div>
        <div className="live">
          <span className="dot" />
          live
        </div>
        <div className="count">
          <b>{events.length}</b> events · <b>{blocked}</b> blocked
        </div>
        <button
          className={`stats-btn${showStats ? " on" : ""}`}
          onClick={() => setShowStats((v) => !v)}
          aria-pressed={showStats}
        >
          {showStats ? "▾ Stats" : "▸ Stats"}
        </button>
        <button className="undo-btn" onClick={undo} disabled={undoing}>
          {undoing ? "Reverting…" : "↺ Undo last change"}
        </button>
      </div>

      {showStats && <StatsPanel stats={stats} />}

      {toast && (
        <div className="toast">
          <b>›</b> {toast}
        </div>
      )}

      {events.length === 0 ? (
        <div className="empty">
          waiting for events
          <span className="cursor" />
        </div>
      ) : (
        <div className="timeline">
          {events.map((ev) => (
            <EventCard key={ev.id} ev={ev} />
          ))}
        </div>
      )}
    </main>
  );
}
