"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type LeashEvent = {
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

function EventCard({ ev }: { ev: LeashEvent }) {
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
  const [events, setEvents] = useState<LeashEvent[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);
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

  return (
    <main className="shell">
      <div className="topbar">
        <div className="brand">
          <b>leash</b>
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
