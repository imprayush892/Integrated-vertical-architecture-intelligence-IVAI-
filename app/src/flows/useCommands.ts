// Builds the palette's command list from the existing store actions and RPC calls.
// Nothing here owns new behaviour: each command mirrors what a button in the app already does.
import { useMemo } from "react";
import { dxfUrl, rpc } from "../lib/api";
import { useStore, waiting } from "../lib/store";
import { blockedReason, STAGES } from "../shell/stages";
import type { ThemePref } from "../shell/theme";
import type { Command } from "./CommandPalette";

export interface CommandHooks {
  theme: { pref: ThemePref; resolved: "light" | "dark"; setPref: (p: ThemePref) => void };
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  onOpenLink: () => void;
  onOpenSettings: () => void;
  onOpenHelp: () => void;
  onReplayTour: () => void;
}

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function useCommands(h: CommandHooks): Command[] {
  const project = useStore((s) => s.project);
  const step = useStore((s) => s.step);
  const tickets = useStore((s) => s.tickets);
  const inboxOpen = useStore((s) => s.inboxOpen);
  const busy = useStore((s) => s.busy);
  const bridge = useStore((s) => s.bridge);
  const { inspectorOpen, onToggleInspector, onOpenLink, onOpenSettings, onOpenHelp, onReplayTour } = h;

  const { pref, resolved, setPref } = h.theme;

  return useMemo<Command[]>(() => {
    const st = useStore.getState();
    const waitingN = waiting(tickets).length;
    const busyReason = busy ? `Wait for “${busy}” to finish.` : null;
    const currentStage = STAGES.find((s) => s.steps.includes(step))?.n ?? 1;

    const stageCmds: Command[] = STAGES.map((meta) => {
      const blocked = blockedReason(meta.n, project);
      const isCurrent = meta.n === currentStage;
      const done = meta.n < (project?.stage ?? 1);
      return {
        id: `stage-${meta.n}`,
        title: `Go to ${meta.n} · ${meta.label}`,
        detail: meta.title,
        group: "Stages",
        icon: "layers",
        keywords: `stage ${meta.n} ${meta.label} ${meta.title}`,
        disabled: blocked,
        keys: [String(meta.n)],
        badge: isCurrent ? { text: "current", led: "cobalt" } : done ? { text: "done", led: "det" } : undefined,
        run: () => { if (!meta.steps.includes(useStore.getState().step)) useStore.getState().setStep(meta.steps[0]); },
      };
    });

    const solveBlocked = busyReason ?? (project?.units.length ? null : "Shape a plate and split it into flats first.");
    const checksBlocked = busyReason ?? blockedReason(8, project);
    const exportBlocked = project?.plates.length || project?.plans.length ? null : "Choose a tower placement first.";
    const undoBlocked = busyReason ?? (project ? null : "Open a project first.");

    const actions: Command[] = [
      {
        id: "solve-all", title: "Solve all flats", detail: "Rules pack every room into its flat.", group: "Actions", icon: "play",
        keywords: "run solve pack layout wire rooms", by: "engine", disabled: solveBlocked,
        run: async () => {
          const out = await st.run("Solving all flats", () => rpc<{ solved: number; total: number }>("solve_all", {}));
          if (out && out.solved > 0) { st.toast("ok", `${out.solved} of ${out.total} flats solved.`); st.setStep("review"); }
          else if (out) st.toast("error", "No flat could be solved. Open stage 7 to see why.");
        },
      },
      {
        id: "run-checks", title: "Run the rule checks", detail: "DCPR 2034 and NBC 2016 on every room, door and window.", group: "Actions", icon: "check",
        keywords: "check review verify dcpr nbc compliance", by: "engine", disabled: checksBlocked,
        run: async () => {
          const out = await st.run("Running checks", () => rpc("run_checks"));
          if (out) { st.toast("ok", "Checks finished."); st.setStep("review"); }
        },
      },
      {
        id: "export-dxf", title: "Export DXF", detail: "Download the plan as a drawing file.", group: "Actions", icon: "download",
        keywords: "download save cad drawing dxf export", by: "engine", disabled: exportBlocked,
        run: async () => {
          try {
            window.location.href = await dxfUrl(useStore.getState().tower);
            st.toast("info", "Your DXF download is starting.");
          } catch (e) { st.toast("error", errText(e)); }
        },
      },
      {
        id: "undo", title: "Undo the last change", detail: "Steps the project back one saved version.", group: "Actions", icon: "undo",
        keywords: "revert back", disabled: undoBlocked,
        run: () => { void st.run("Undo", () => rpc("undo")); },
      },
      {
        id: "inbox", title: inboxOpen ? "Close the decision inbox" : "Open the decision inbox", group: "Actions", icon: "inbox",
        detail: waitingN ? "Claude's tickets wait here for Keep or Undo." : "No tickets waiting.",
        keywords: "decisions tickets claude keep undo", by: "claude",
        badge: waitingN ? { text: `${waitingN} waiting`, led: "ai" } : undefined,
        run: () => useStore.getState().setInbox(!useStore.getState().inboxOpen),
      },
      {
        id: "link-claude", title: bridge.linked && (bridge.fresh ?? true) ? "Claude link settings" : "Link Claude", group: "Actions", icon: "link",
        detail: "Lets Claude use the planner's tools. It only ever proposes.", keywords: "connect mcp co-pilot assistant ai", by: "claude",
        badge: bridge.linked && (bridge.fresh ?? true) ? { text: "linked", led: "ai" } : undefined,
        run: onOpenLink,
      },
      { id: "settings", title: "Open settings", group: "Actions", icon: "settings", keywords: "preferences map imagery token", run: onOpenSettings },
    ];

    const view: Command[] = [
      {
        id: "theme-toggle", title: resolved === "dark" ? "Switch to the light theme" : "Switch to the dark theme", group: "View",
        icon: resolved === "dark" ? "sun" : "moon", keywords: "theme dark light mode appearance colour",
        run: () => setPref(resolved === "dark" ? "light" : "dark"),
      },
      ...(pref !== "system" ? [{
        id: "theme-system", title: "Match the system theme", group: "View" as const, icon: "monitor" as const,
        keywords: "theme auto system", run: () => setPref("system"),
      }] : []),
      {
        id: "inspector", title: inspectorOpen ? "Hide the inspector" : "Show the inspector", group: "View", icon: "panel-right",
        detail: "Rules, co-pilot roles and figures for this stage.", keywords: "panel sidebar right", run: onToggleInspector,
      },
    ];

    const help: Command[] = [
      { id: "shortcuts", title: "Keyboard shortcuts", group: "Help", icon: "keyboard", keys: ["?"], keywords: "keys hotkeys help", run: onOpenHelp },
      { id: "tour", title: "Replay the quick tour", group: "Help", icon: "info", detail: "Four short steps over the stage keys.", keywords: "onboarding intro guide help", run: onReplayTour },
    ];

    return [...stageCmds, ...actions, ...view, ...help];
  }, [project, step, tickets, inboxOpen, busy, bridge, pref, resolved, setPref, inspectorOpen, onToggleInspector, onOpenLink, onOpenSettings, onOpenHelp, onReplayTour]);
}
