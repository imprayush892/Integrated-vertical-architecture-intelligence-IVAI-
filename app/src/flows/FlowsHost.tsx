// Mount point for the cross-cutting flows: onboarding, command palette, shortcuts overlay, engine banner.
// App.tsx renders <FlowsHost .../> once and wraps itself in <ErrorBoundary>.
import { useCallback, useEffect, useState } from "react";
import { useStore } from "../lib/store";
import type { ThemePref } from "../shell/theme";
import { CommandPalette } from "./CommandPalette";
import { OfflineBanner } from "./OfflineBanner";
import { Onboarding } from "./Onboarding";
import { Shortcuts } from "./Shortcuts";
import { isOnboarded, isTyping, markOnboarded, otherModalOpen } from "./storage";
import { useCommands } from "./useCommands";
import "./flows.css";

export interface FlowsHostProps {
  theme: { pref: ThemePref; resolved: "light" | "dark"; setPref: (p: ThemePref) => void };
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  onOpenLink: () => void;
  onOpenSettings: () => void;
}

export function FlowsHost({ theme, inspectorOpen, onToggleInspector, onOpenLink, onOpenSettings }: FlowsHostProps) {
  const project = useStore((s) => s.project);
  const toast = useStore((s) => s.toast);
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const [tour, setTour] = useState(false);

  // first run: only for someone who has not traced a plot yet
  const hasProject = !!project;
  const hasSite = !!project?.site;
  useEffect(() => {
    if (!hasProject || hasSite || isOnboarded()) return;
    const t = setTimeout(() => { if (!otherModalOpen()) setTour(true); }, 700);
    return () => clearTimeout(t);
  }, [hasProject, hasSite]);

  const closeTour = useCallback(() => { setTour(false); markOnboarded(); }, []);
  const openPalette = useCallback(() => { closeTour(); setHelp(false); setPalette(true); }, [closeTour]);
  const openHelp = useCallback(() => { closeTour(); setPalette(false); setHelp(true); }, [closeTour]);
  const replay = useCallback(() => { setPalette(false); setHelp(false); setTour(true); }, []);

  // global keys: Ctrl/Cmd+K toggles the palette (works inside fields), ? opens the shortcut list
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        if (otherModalOpen()) return;
        e.preventDefault();
        setPalette((o) => !o);
        closeTour();
        return;
      }
      if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey && !e.defaultPrevented) {
        if (isTyping(e.target) || otherModalOpen()) return;
        e.preventDefault();
        openHelp();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openHelp, closeTour]);

  const commands = useCommands({
    theme, inspectorOpen, onToggleInspector, onOpenLink, onOpenSettings, onOpenHelp: openHelp, onReplayTour: replay,
  });

  return (
    <>
      <OfflineBanner />
      <Onboarding open={tour} onClose={closeTour} />
      <CommandPalette open={palette} onClose={() => setPalette(false)} commands={commands} onExplain={(r) => toast("info", r)} />
      <Shortcuts open={help} onClose={() => setHelp(false)} onOpenPalette={openPalette} />
    </>
  );
}
