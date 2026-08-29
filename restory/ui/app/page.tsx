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
};

function fmtTime(ts: string): string {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function fmtFull(ts: string): string {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts || "—";
  return d.toLocaleString("en-GB", { hour12: false });
}

function EventCard({
  ev,
  expanded,
  onToggle,
}: {
  ev: RestoryEvent;
  expanded: boolean;
  onToggle: (id: number) => void;
}) {
  const isFileOp = ["Write", "Edit", "MultiEdit"].includes(ev.tool_name);
  const sigil = isFileOp ? "±" : "$";
  return (
    <div className="event">
      <span className={`node${ev.danger ? " danger" : ""}`} />
      <div
        className={`card${ev.danger ? " danger" : ""}${expanded ? " open" : ""}`}
        onClick={() => onToggle(ev.id)}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle(ev.id);
          }
        }}
      >
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
            <span className="chev" aria-hidden="true">
              {expanded ? "▾" : "▸"}
            </span>
          </div>
          <code className={`cmd${ev.danger ? " danger" : ""}${expanded ? " full" : ""}`}>
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

          {expanded && (
            <dl className="details" onClick={(e) => e.stopPropagation()}>
              <dt>Tool</dt>
              <dd>{ev.tool_name || "—"}</dd>
              <dt>Timestamp</dt>
              <dd>{fmtFull(ev.timestamp)}</dd>
              {ev.event && (
                <>
                  <dt>Event</dt>
                  <dd>{ev.event}</dd>
                </>
              )}
              {ev.decision && (
                <>
                  <dt>Decision</dt>
                  <dd>{ev.decision}</dd>
                </>
              )}
              <dt>Reason</dt>
              <dd>{ev.reason || "—"}</dd>
              <dt>Tags</dt>
              <dd>
                {ev.tags.length > 0 ? (
                  <span className="detail-tags">
                    {ev.tags.map((t) => (
                      <span key={t} className="badge danger">
                        {t}
                      </span>
                    ))}
                  </span>
                ) : (
                  "—"
                )}
              </dd>
              <dt>Command</dt>
              <dd>
                <code className="detail-cmd">{ev.command || "(no command)"}</code>
              </dd>
            </dl>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Home() {
  const [events, setEvents] = useState<RestoryEvent[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- client-side filter state (all applied to already-fetched events) ---
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

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

  const toggleExpand = useCallback((id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const blocked = events.filter((e) => e.danger).length;

  // Stable set of all tags seen, so the chip row doesn't jump as filters change.
  const allTags = useMemo(() => {
    const set = new Set<string>();
    for (const ev of events) for (const t of ev.tags) set.add(t);
    return Array.from(set).sort();
  }, [events]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter((ev) => {
      if (blockedOnly && !ev.danger) return false;
      if (activeTag && !ev.tags.includes(activeTag)) return false;
      if (q) {
        const hay = `${ev.command} ${ev.reason}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [events, blockedOnly, activeTag, query]);

  const filtersActive = blockedOnly || activeTag !== null || query.trim() !== "";

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
        <button className="undo-btn" onClick={undo} disabled={undoing}>
          {undoing ? "Reverting…" : "↺ Undo last change"}
        </button>
      </div>

      <div className="filterbar">
        <div className="filter-row">
          <button
            className={`chip toggle${blockedOnly ? " active" : ""}`}
            onClick={() => setBlockedOnly((v) => !v)}
            aria-pressed={blockedOnly}
          >
            ⛔ blocked only
          </button>
          <input
            className="search"
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search command or reason…"
            spellCheck={false}
            aria-label="Search events"
          />
          {query && (
            <button
              className="clear-search"
              onClick={() => setQuery("")}
              aria-label="Clear search"
            >
              ✕
            </button>
          )}
          {filtersActive && (
            <span className="filter-count">
              {filtered.length}/{events.length} shown
            </span>
          )}
        </div>
        {allTags.length > 0 && (
          <div className="filter-row tags-row">
            <button
              className={`chip tag${activeTag === null ? " active" : ""}`}
              onClick={() => setActiveTag(null)}
            >
              all tags
            </button>
            {allTags.map((t) => (
              <button
                key={t}
                className={`chip tag${activeTag === t ? " active" : ""}`}
                onClick={() => setActiveTag((cur) => (cur === t ? null : t))}
                aria-pressed={activeTag === t}
              >
                {t}
              </button>
            ))}
          </div>
        )}
      </div>

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
      ) : filtered.length === 0 ? (
        <div className="empty">no events match the current filters</div>
      ) : (
        <div className="timeline">
          {filtered.map((ev) => (
            <EventCard
              key={ev.id}
              ev={ev}
              expanded={expanded.has(ev.id)}
              onToggle={toggleExpand}
            />
          ))}
        </div>
      )}
    </main>
  );
}
