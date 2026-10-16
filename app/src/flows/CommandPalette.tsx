// Command palette (Ctrl/Cmd+K). Generic UI: the commands are built in useCommands.ts.
import { type KeyboardEvent, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon, type IconName, Kbd, Led, ProvenanceTag, Readout, type Actor } from "../ui";
import { pushRecent, readRecent } from "./storage";
import "./flows.css";

export type CommandGroup = "Stages" | "Actions" | "View" | "Help";

export interface Command {
  id: string;
  title: string;
  /** One line under the title (what it does). Replaced by `disabled` when the command cannot run. */
  detail?: string;
  group: CommandGroup;
  icon: IconName;
  /** Extra search words. */
  keywords?: string;
  /** Why the command cannot run right now (null/undefined = available). Selecting it explains instead of failing. */
  disabled?: string | null;
  /** Provenance: engine = deterministic rules, claude = co-pilot. */
  by?: Actor;
  /** Shortcut hint shown on the right. */
  keys?: string[];
  /** Short status chip on the right, e.g. "current" or "3 waiting". */
  badge?: { text: string; led?: "det" | "ai" | "cobalt" | "idle" };
  run: () => void | Promise<void>;
}

const GROUP_ORDER: CommandGroup[] = ["Actions", "Stages", "View", "Help"];

function score(c: Command, q: string): number {
  const title = c.title.toLowerCase();
  const hay = `${title} ${c.group.toLowerCase()} ${c.keywords ?? ""} ${c.detail ?? ""}`.toLowerCase();
  let total = 0;
  for (const tok of q.split(/\s+/).filter(Boolean)) {
    const at = title.indexOf(tok);
    if (at === 0) total += 4;
    else if (at > 0) total += title[at - 1] === " " ? 3 : 2;
    else if (hay.includes(tok)) total += 1;
    else {
      // loose subsequence match on the title ("sdxf" -> "Save DXF")
      let i = 0;
      for (const ch of title) if (ch === tok[i]) i++;
      if (i < tok.length) return -1;
      total += 0.5;
    }
  }
  return total;
}

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  commands: Command[];
  /** Called when a disabled command is chosen, with its reason. */
  onExplain: (reason: string) => void;
}

export function CommandPalette({ open, onClose, commands, onExplain }: CommandPaletteProps) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const uid = useId();
  const listId = `${uid}-list`;

  // fresh query every time it opens; restore focus on close
  useEffect(() => {
    if (!open) return;
    setQ(""); setActive(0);
    const prev = document.activeElement as HTMLElement | null;
    requestAnimationFrame(() => inputRef.current?.focus());
    return () => { prev?.focus?.(); };
  }, [open]);

  const rows = useMemo(() => {
    const query = q.trim().toLowerCase();
    if (query) {
      return commands
        .map((c, i) => ({ c, s: score(c, query), i }))
        .filter((x) => x.s >= 0)
        .sort((a, b) => b.s - a.s || a.i - b.i)
        .map((x) => ({ c: x.c, group: null as string | null }));
    }
    const out: { c: Command; group: string | null }[] = [];
    const byId = new Map(commands.map((c) => [c.id, c]));
    const recent = readRecent().map((id) => byId.get(id)).filter((c): c is Command => !!c);
    recent.forEach((c, i) => out.push({ c, group: i === 0 ? "Recent" : null }));
    for (const g of GROUP_ORDER) {
      commands.filter((c) => c.group === g).forEach((c, i) => out.push({ c, group: i === 0 ? g : null }));
    }
    return out;
  }, [commands, q, open]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { setActive(0); }, [q]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;

  const choose = (c: Command) => {
    if (c.disabled) { onExplain(c.disabled); return; }
    pushRecent(c.id);
    onClose();
    // let focus restore and the dialog unmount first, so a command that opens a dialog keeps its own focus
    setTimeout(() => { void Promise.resolve(c.run()); }, 0);
  };

  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (rows.length ? (a + 1) % rows.length : 0)); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (rows.length ? (a - 1 + rows.length) % rows.length : 0)); return; }
    if (e.key === "Enter") { e.preventDefault(); const r = rows[active]; if (r) choose(r.c); return; }
    if (e.key === "Tab") e.preventDefault(); // keep focus in the search box; the list is driven by the arrow keys
  };

  const activeId = rows[active] ? `${uid}-opt-${active}` : undefined;
  const status = q.trim() ? `${rows.length} command${rows.length === 1 ? "" : "s"} match` : `${commands.length} commands`;

  return createPortal(
    <div className="ui-scrim fx-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Command palette" data-fx-own tabIndex={-1}
        className="fx-palette neo-flat" onKeyDown={onKey}>
        <div className="fx-search">
          <Icon name="search" size={18} />
          <input ref={inputRef} type="text" role="combobox" aria-expanded="true" aria-controls={listId} aria-autocomplete="list"
            aria-activedescendant={activeId} aria-label="Type a command" placeholder="Type a command or a stage name"
            autoComplete="off" spellCheck={false} value={q} onChange={(e) => setQ(e.target.value)} />
          <Kbd>Esc</Kbd>
        </div>
        <div ref={listRef} id={listId} role="listbox" aria-label="Commands" className="fx-list neo-well">
          {rows.length === 0 && (
            <div className="fx-none" role="presentation">
              <Icon name="search" size={20} />
              <p className="fx-none-title">Nothing matches “{q.trim()}”</p>
              <p className="fx-none-hint">Try a stage name such as “check”, or an action such as “export”.</p>
            </div>
          )}
          {rows.map((r, idx) => (
            <div key={`${idx}-${r.c.id}`} role="presentation">
              {r.group && <div className="fx-group" role="presentation">{r.group}</div>}
              <Option id={`${uid}-opt-${idx}`} idx={idx} cmd={r.c} active={idx === active}
                onHover={() => setActive(idx)} onPick={() => choose(r.c)} />
            </div>
          ))}
        </div>
        <div className="fx-palette-foot">
          <span className="fx-foot-keys" aria-hidden="true">
            <span><Kbd>↑</Kbd><Kbd>↓</Kbd> move</span><span><Kbd>Enter</Kbd> run</span>
          </span>
          <Readout label="Showing" value={rows.length} digits={0} tone="cobalt" size="sm" layout="inline" />
          <span className="sr-only" role="status" aria-live="polite">{status}</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function Option({ id, idx, cmd, active, onHover, onPick }: { id: string; idx: number; cmd: Command; active: boolean; onHover: () => void; onPick: () => void }) {
  const dis = !!cmd.disabled;
  let right: ReactNode = null;
  if (cmd.badge) right = <span className="fx-badge"><Led status={cmd.badge.led ?? "idle"} />{cmd.badge.text}</span>;
  return (
    <div id={id} role="option" aria-selected={active} aria-disabled={dis || undefined} data-idx={idx}
      className={`fx-item${active ? " is-active" : ""}${dis ? " is-disabled" : ""}`} onMouseMove={onHover} onClick={onPick}>
      <span className="fx-item-icon" aria-hidden="true"><Icon name={dis ? "lock" : cmd.icon} size={16} /></span>
      <span className="fx-item-text">
        <span className="fx-item-title">{cmd.title}</span>
        {(dis || cmd.detail) && <span className="fx-item-detail">{dis ? cmd.disabled : cmd.detail}</span>}
      </span>
      <span className="fx-item-meta">
        {cmd.by && !dis && <ProvenanceTag by={cmd.by} compact />}
        {right}
        {cmd.keys && <span className="fx-item-keys" aria-hidden="true">{cmd.keys.map((k) => <Kbd key={k}>{k}</Kbd>)}</span>}
      </span>
    </div>
  );
}
