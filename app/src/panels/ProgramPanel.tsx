import { useCallback, useEffect, useMemo, useState } from "react";
import { rpc } from "../lib/api";
import { fmt, ROOM_COLORS } from "../lib/geom";
import { useStore } from "../lib/store";
import type { Programme, ProgrammeRoom, Ticket } from "../lib/types";
import { Channel, Chip, EmptyState, Icon, Led, LatchButton, Panel, ProvenanceTag, Readout, Select, Skeleton, TextArea, Toggle } from "../ui";

// ------------------------------------------------------------------------------------------- 6b Programme
/** What the engine accepts: no room above 90% of the flat, and all targets together at most 1.4 x that. */
const USABLE = 0.9;
const TOTAL_FACTOR = 1.4;

const CSS = `
.prg-root { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.prg-root h2 { margin: 0; }
.prg-lede { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; margin: 0; }
.prg-hint { font-size: 12px; color: var(--ink-3); line-height: 1.4; margin: 0; }
.prg-row-meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.prg-rooms { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.prg-room { border-radius: var(--r-md); border: 1px solid var(--chamfer); background: var(--neo-convex-bg); box-shadow: var(--neo-raised-xs); }
.prg-room.is-off { background: var(--neo-bg); box-shadow: var(--neo-latched); }
.prg-room.is-open { box-shadow: var(--neo-raised-sm); }
.prg-room-head { appearance: none; background: transparent; border: 0; width: 100%; display: flex; align-items: center; gap: 10px; padding: 9px 10px; text-align: left; color: var(--ink-1); cursor: pointer; border-radius: var(--r-md); }
.prg-sw { width: 12px; height: 12px; border-radius: 4px; flex: none; box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--ink-3) 55%, transparent); }
.prg-name { font: 700 13px var(--f-sans); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prg-room.is-off .prg-name { color: var(--ink-3); text-decoration: line-through; }
.prg-lcds { display: flex; gap: 6px; flex: none; }
.prg-lcds .lcd { min-width: 0; padding: 3px 8px; font-size: 0.74rem; }
.prg-lcds .lcd.is-edited { outline: 1.5px solid var(--cobalt); outline-offset: 0; }
.prg-chev { color: var(--ink-3); flex: none; transition: transform var(--dur-2) var(--ease); }
.prg-room.is-open .prg-chev { transform: rotate(180deg); }
.prg-room-body { display: flex; flex-direction: column; gap: 10px; padding: 2px 10px 12px; }
.prg-room-body .ui-channel { grid-template-columns: minmax(0, 1fr) auto; row-gap: 6px; box-shadow: var(--neo-latched); }
.prg-room-body .ui-channel-range { grid-column: 1 / -1; grid-row: 2; }
.prg-tot { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.prg-fit { display: flex; align-items: flex-start; gap: 8px; font: 600 11.5px/1.45 var(--f-mono); color: var(--ink-2); }
.prg-fit.bad { color: var(--fail-ink); }
.prg-fit .led { margin-top: 5px; }
.prg-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.prg-actions > * { flex: 1 1 120px; }
.prg-quote { margin: 0; padding: 8px 12px; border-radius: var(--r-md); box-shadow: var(--neo-latched); font-size: 13px; line-height: 1.5; }
.prg-wait { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.prg-err { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 12.5px; color: var(--fail-ink); }
.prg-skel { display: flex; flex-direction: column; gap: 8px; }
.prg-prop { display: flex; flex-direction: column; gap: 10px; }
.prg-more summary { cursor: pointer; font: 600 12px var(--f-sans); color: var(--ink-2); padding: 2px; border-radius: 6px; }
.prg-more p { margin-top: 6px; }
.prg-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.prg-tiles .ui-btn-tile { min-width: 0; }
`;

interface Edit { target: number; min: number; on: boolean }
type Edits = Record<string, Edit>;

const roomSwatch = (type: string) => ROOM_COLORS[type] ?? "var(--room-other)";

/** The newest T8 ticket for a flat (ids are `T8:<unit>`; fall back to subject match). */
function t8For(tickets: Ticket[], unitId: string): Ticket | undefined {
  const mine = tickets.filter((t) => t.kind === "T8" && (t.subject === unitId || t.id === `T8:${unitId}`));
  return mine[mine.length - 1];
}

export function ProgramPanel() {
  const project = useStore((s) => s.project);
  const unitId = useStore((s) => s.unit);
  const setUnit = useStore((s) => s.setUnit);
  const run = useStore((s) => s.run);
  const setStep = useStore((s) => s.setStep);
  const busy = useStore((s) => s.busy);
  const tickets = useStore((s) => s.tickets);
  const bridge = useStore((s) => s.bridge);
  const toast = useStore((s) => s.toast);
  const [presets, setPresets] = useState<Programme[]>([]);
  const [presetState, setPresetState] = useState<"loading" | "ready" | "error">("loading");
  const [brief, setBrief] = useState("");
  const [edits, setEdits] = useState<Edits>({});
  const [open, setOpen] = useState<string | null>(null);
  const [said, setSaid] = useState("");

  const loadPresets = useCallback(() => {
    setPresetState("loading");
    rpc<Programme[]>("list_presets").then((p) => { setPresets(p); setPresetState("ready"); }).catch(() => setPresetState("error"));
  }, []);
  useEffect(() => { loadPresets(); }, [loadPresets]);

  const units = project?.units ?? [];
  const u = units.find((x) => x.id === unitId) ?? units[0];
  const prog = u?.programme ?? null;
  const progKey = prog ? `${u?.id}|${prog.id}|${prog.rooms.map((r) => `${r.id}:${r.target_area}:${r.min_area}`).join(",")}` : `${u?.id}|none`;

  // a different flat or a new programme resets the hand edits
  useEffect(() => { setEdits({}); setOpen(null); }, [progKey]);

  const rooms = prog?.rooms ?? [];
  const edit = (r: ProgrammeRoom): Edit => edits[r.id] ?? { target: r.target_area, min: r.min_area, on: true };
  const dirty = rooms.some((r) => {
    const e = edits[r.id];
    return !!e && (e.target !== r.target_area || e.min !== r.min_area || !e.on);
  });
  const included = rooms.filter((r) => edit(r).on);
  const total = included.reduce((a, r) => a + edit(r).target, 0);
  const usable = (u?.area ?? 0) * USABLE;
  const limit = usable * TOTAL_FACTOR;
  const biggest = included.reduce((m, r) => Math.max(m, edit(r).target), 0);
  const tooBig = !!u && (biggest > usable || total > limit);

  const noteParts = (prog?.note ?? "").split(/(?<=\.)\s+/);
  const noteHead = noteParts[0] ?? "";
  const noteMore = noteParts.slice(1).join(" ");
  const ticket = u ? t8For(tickets, u.id) : undefined;
  const isBusy = busy !== null;
  const prov: { by: "engine" | "claude" | "user"; text: string } = useMemo(() => {
    if (!prog) return { by: "engine", text: "No room list yet." };
    const answered = ticket && (ticket.status === "answered" || ticket.status === "kept") && prog.id.startsWith("custom");
    if (answered && ticket.answer?.by === "claude") return { by: "claude", text: "Written by Claude from your brief, then checked by the rules." };
    if (answered && ticket.answer?.by === "user") return { by: "user", text: "Edited by you, then checked by the rules." };
    if (prog.id.startsWith("custom")) return { by: "user", text: "A custom room list." };
    return { by: "engine", text: "A preset from the rules files." };
  }, [prog, ticket]);

  if (!u) {
    return (
      <div className="prg-root">
        <style>{CSS}</style>
        <h2>Room lists</h2>
        <EmptyState icon={<Icon name="grid" size={24} />} title="No flats yet"
          action={<LatchButton variant="primary" onClick={() => setStep("units")}>Split into flats</LatchButton>}>
          Split the plate first. Each flat gets its own room list.
        </EmptyState>
      </div>
    );
  }

  const setEdit = (r: ProgrammeRoom, patch: Partial<Edit>) => setEdits((cur) => ({ ...cur, [r.id]: { ...edit(r), ...patch } }));

  const apply = async () => {
    if (!prog || tooBig) return;
    const ids = new Set(included.map((r) => r.id));
    const out = await run("Checking room list", () => rpc("propose_programme", {
      unit_id: u.id,
      brief: `Edited ${u.id}: ${prog.name}`.slice(0, 60),
      rooms: included.map((r) => ({ ...r, target_area: edit(r).target, min_area: Math.min(edit(r).min, edit(r).target) })),
      links: prog.links.filter(([a, b]) => ids.has(a) && ids.has(b)),
      entry: ids.has(prog.entry) ? prog.entry : undefined,
      by: "user",
    }));
    if (out !== undefined) { setSaid("Room list applied."); toast("ok", `Room list for ${u.id} applied. The rules accepted it.`); }
  };

  const keep = () => ticket && run("Keeping", () => rpc("review_decision", { ticket_id: ticket.id, keep: true }));
  const undo = async () => {
    if (!ticket) return;
    // The engine's Undo re-applies the safe default, which leaves the programme as is. Put the previous
    // preset back too when we can tell which one it was (the ticket's default option is named after it).
    const prev = presets.find((p) => p.name === ticket.options.find((o) => o.id === ticket.default_option)?.label);
    const out = await run("Undoing", () => rpc("review_decision", { ticket_id: ticket.id, keep: false }));
    if (out !== undefined && prev && useStore.getState().project?.units.find((x) => x.id === u.id)?.programme?.id !== prev.id) {
      await run("Restoring room list", () => rpc("set_programme", { unit_id: u.id, preset: prev.id }));
    }
  };

  const waitingOnClaude = !!ticket && (ticket.status === "open" || ticket.status === "defaulted");
  const proposed = !!ticket && ticket.status === "answered";
  const claudeLinked = bridge.linked && (bridge.fresh ?? true);

  return (
    <div className="prg-root">
      <style>{CSS}</style>
      <h2>Room lists</h2>

      <Select label="Flat" value={u.id} onChange={(e) => setUnit(e.target.value)} disabled={isBusy}
        options={units.map((x) => ({ value: x.id, label: `${x.id} · ${fmt(x.area)} m²${x.programme ? ` · ${x.programme.bhk} BHK` : ""}` }))} />

      <Panel profile="well" pad="sm" title="Room list"
        actions={<ProvenanceTag by={prov.by} kind={prov.by === "engine" ? "derived" : prov.by === "claude" ? "inferred" : "observed"} source={prov.text} compact />}>
        {presetState === "error" && (
          <div className="prg-err" role="alert"><span>Could not load the presets.</span><LatchButton size="sm" onClick={loadPresets}>Try again</LatchButton></div>
        )}
        {presetState === "loading" ? <Skeleton height={38} radius={12} /> : (
          <Select label="Preset" value={prog?.id ?? ""} disabled={isBusy || presetState === "error"}
            onChange={(e) => { const v = e.target.value; if (v) void run("Setting room list", () => rpc("set_programme", { unit_id: u.id, preset: v })); }}>
            {!prog && <option value="">Choose a preset</option>}
            {presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            {prog && !presets.some((p) => p.id === prog.id) && <option value={prog.id}>{prog.name}</option>}
          </Select>
        )}
        {prog && (
          <div className="prg-row-meta">
            <Chip led={prov.by === "claude" ? "ai" : "det"} inset>{prog.bhk} BHK</Chip>
            <Chip inset>{rooms.length} rooms</Chip>
            <Chip inset>Entry: {rooms.find((r) => r.id === prog.entry)?.name ?? prog.entry}</Chip>
          </div>
        )}
        <p className="prg-hint">{prov.by === "claude" ? <span className="backlit">{prov.text}</span> : prov.text} {noteHead}</p>
        {noteMore && (
          <details className="prg-more"><summary>Where these numbers come from</summary><p className="prg-hint">{noteMore}</p></details>
        )}
      </Panel>

      {prog ? (
        <Panel profile="flat" pad="sm" small title="Rooms and areas"
          actions={dirty ? <Chip led="cobalt" inset>Edited</Chip> : undefined}>
          <p className="prg-lede">Open a room to set its target and minimum area. Statutory minimums are checked in stage 8, from the rules files.</p>
          <ul className="prg-rooms" aria-label="Rooms in this flat">
            {rooms.map((r) => {
              const e = edit(r);
              const isOpen = open === r.id;
              const touched = !!edits[r.id] && (e.target !== r.target_area || e.min !== r.min_area);
              return (
                <li key={r.id} className={`prg-room${isOpen ? " is-open" : ""}${e.on ? "" : " is-off"}`}>
                  <button type="button" className="prg-room-head" aria-expanded={isOpen} aria-controls={`prg-${r.id}`} onClick={() => setOpen(isOpen ? null : r.id)}>
                    <span className="prg-sw" style={{ background: roomSwatch(r.type) }} aria-hidden="true" />
                    <span className="prg-name">{r.name}{r.optional ? " (optional)" : ""}{!e.on ? ", left out" : ""}</span>
                    <span className="prg-lcds">
                      <span className={`lcd det${touched ? " is-edited" : ""}`} aria-label={`Target ${fmt(e.target)} square metres`}>{fmt(e.target)}<span className="lcd-unit">m²</span></span>
                    </span>
                    <Icon name="chevron-down" size={14} className="prg-chev" />
                  </button>
                  {isOpen && (
                    <div className="prg-room-body" id={`prg-${r.id}`}>
                      <div className="prg-row-meta">
                        <Chip inset>{r.type}</Chip>
                        {r.needs_window && <Chip led="det" inset>Needs a window</Chip>}
                      </div>
                      <Channel label="Target area" tone="cobalt" value={e.target} min={Math.max(1, Math.min(e.min, e.target))} max={Math.max(40, Math.ceil(r.target_area * 2))} step={0.5} unit="m²"
                        hint="What the solver aims for" disabled={!e.on || isBusy}
                        onChange={(v) => setEdit(r, { target: v, min: Math.min(e.min, v) })} />
                      <Channel label="Minimum area" tone="cobalt" value={Math.min(e.min, e.target)} min={0} max={Math.max(1, e.target)} step={0.5} unit="m²"
                        hint="Never go below this" disabled={!e.on || isBusy}
                        onChange={(v) => setEdit(r, { min: v })} />
                      {r.optional && (
                        <Toggle label="Include this room" checked={e.on} onChange={(on) => setEdit(r, { on })} />
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          <div className="prg-tot">
            <Readout label="Targets" value={total} unit="m²" tone={tooBig ? "fail" : "ink"} size="sm" />
            <Readout label="Flat" value={u.area} unit="m²" tone="det" size="sm" />
            <Readout label="Accepts up to" value={limit} unit="m²" tone="ink" size="sm" />
          </div>
          <div className={`prg-fit${tooBig ? " bad" : ""}`} role="status">
            <Led status={tooBig ? "fail" : "det"} />
            <span>{tooBig
              ? `Too much for this flat. No room above ${fmt(usable)} m², and targets together at most ${fmt(limit)} m².`
              : "Fits this flat. The rules re-check it when you apply."}</span>
          </div>
          <div className="prg-actions">
            <LatchButton variant="det" disabled={!dirty || tooBig || isBusy} loading={busy === "Checking room list"} onClick={() => void apply()}>Apply room list</LatchButton>
            <LatchButton disabled={!dirty || isBusy} onClick={() => { setEdits({}); setSaid("Edits discarded."); }}>Reset</LatchButton>
          </div>
        </Panel>
      ) : (
        <EmptyState icon={<Icon name="grid" size={22} />} title="No room list for this flat">Pick a preset above to see its rooms.</EmptyState>
      )}

      <Panel profile="flat" pad="sm" small title="Brief in words"
        actions={<ProvenanceTag by="claude" compact kind="inferred" source="Claude turns your words into a room list. The rules check it before it is used." />}>
        {waitingOnClaude && ticket && (
          <div className="prg-prop" role="status">
            <div className="prg-wait"><Led status="ai" pulse /><span className="backlit">Waiting for Claude</span></div>
            <p className="prg-quote">{ticket.question}</p>
            <p className="prg-hint">{claudeLinked
              ? "Ask Claude in your chat to answer the waiting planner decisions. The preset stays until it does."
              : "Claude is not linked. Link it from the top bar, or keep the preset."}</p>
          </div>
        )}
        {proposed && ticket && (
          <div className="prg-prop">
            <div className="prg-wait">
              <Led status="ai" />
              <span><b className="backlit">{ticket.answer?.by === "claude" ? "Claude proposed" : "You proposed"}</b> a room list. It is applied, not yet kept.</span>
            </div>
            {ticket.answer?.reason && <p className="prg-quote">{ticket.answer.reason}</p>}
            <div className="prg-row-meta">
              <Chip led="det" inset>Checked by the rules</Chip>
              {ticket.check_me && <Chip led="ai" inset>Check me</Chip>}
            </div>
            <div className="prg-tiles">
              <LatchButton variant="det" sub="Use this list" disabled={isBusy} onClick={() => void keep()}>Keep</LatchButton>
              <LatchButton variant="danger" sub="Back to the preset" disabled={isBusy} onClick={() => void undo()}>Undo</LatchButton>
            </div>
          </div>
        )}
        {ticket && ticket.status === "kept" && (
          <div className="prg-wait" role="status"><Led status="det" /><span>Kept. {prog?.name} is the list for {u.id}.</span></div>
        )}
        {ticket && ticket.status === "undone" && (
          <div className="prg-wait" role="status"><Led status="idle" /><span>Undone. The preset is back for {u.id}.</span></div>
        )}
        <TextArea label="Describe the flat" rows={2} value={brief} onChange={(e) => setBrief(e.target.value)} placeholder="3BHK with a pooja room for a joint family"
          hint="Creates a decision for Claude. Until it answers, the preset stays." />
        <LatchButton block variant="ai" disabled={!brief.trim() || isBusy || waitingOnClaude} loading={busy === "Asking Claude"}
          onClick={async () => {
            const out = await run("Asking Claude", () => rpc("propose_programme", { unit_id: u.id, brief, by: "user" }));
            if (out !== undefined) { setBrief(""); setSaid("Sent to Claude as a decision."); }
          }}>Send to Claude</LatchButton>
        {waitingOnClaude && <p className="prg-hint">One brief at a time. Wait for the answer, or answer it in the inbox.</p>}
      </Panel>

      <LatchButton block variant="primary" size="lg" disabled={!prog || isBusy} onClick={() => setStep("diagram")}>
        Wire the rooms <Icon name="chevron-right" size={14} />
      </LatchButton>
      <span className="sr-only" role="status" aria-live="polite">{said}</span>
    </div>
  );
}
