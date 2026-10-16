import { IconButton, Icon, LatchButton, Led, Logo } from "../ui";
import { ThemeSwitch } from "./ThemeSwitch";
import type { ThemePref } from "./theme";

export interface TopBarProps {
  projectName: string | undefined;
  /** Changes when a different project is opened (resets the name input). */
  projectKey: string | undefined;
  onRename: (name: string) => void;
  onUndo: () => void;
  waiting: number;
  inboxOpen: boolean;
  onToggleInbox: () => void;
  claudeLinked: boolean;
  onOpenLink: () => void;
  onOpenSettings: () => void;
  theme: ThemePref;
  onTheme: (p: ThemePref) => void;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
}

/** Console bar: brand, project name, undo, decisions inbox, Claude link, theme, settings, inspector. */
export function TopBar(p: TopBarProps) {
  return (
    <header className="topbar">
      <div className="topbar-chassis neo-flat">
        <Logo variant="tile" size={28} wordmark className="topbar-brand" />
        <div className="topbar-project">
          <input className="topbar-name" defaultValue={p.projectName} key={p.projectKey} aria-label="Project name" spellCheck={false}
            onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); if (e.key === "Escape") { e.currentTarget.value = p.projectName ?? ""; e.currentTarget.blur(); } }}
            onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== p.projectName) p.onRename(v); }} />
        </div>
        <span className="spacer" />
        <div className="topbar-controls">
          <IconButton label="Undo the last change" icon={<Icon name="undo" />} onClick={p.onUndo} />
          <LatchButton variant="ai" size="sm" latched={p.inboxOpen} led={p.waiting ? "ai" : "idle"} pulse={p.waiting > 0}
            onClick={p.onToggleInbox} aria-label={`Decision inbox, ${p.waiting} waiting`} className="topbar-inbox">
            <span className="hide-sm">Decisions</span><span className="topbar-count" aria-hidden="true">{p.waiting}</span>
          </LatchButton>
          <LatchButton size="sm" led={false} onClick={p.onOpenLink} className="topbar-claude"
            aria-label={p.claudeLinked ? "Claude linked. Open link settings" : "Claude not linked. Link Claude"}>
            <Led status={p.claudeLinked ? "ai" : "idle"} />
            <span className="hide-sm">{p.claudeLinked ? "Claude linked" : "Link Claude"}</span>
          </LatchButton>
          <ThemeSwitch value={p.theme} onChange={p.onTheme} />
          <IconButton label="Settings" icon={<Icon name="settings" />} onClick={p.onOpenSettings} />
          <IconButton label={p.inspectorOpen ? "Hide inspector" : "Show inspector"} icon={<Icon name="panel-right" />}
            pressed={p.inspectorOpen} onClick={p.onToggleInspector} className="topbar-inspector" />
        </div>
      </div>
    </header>
  );
}
