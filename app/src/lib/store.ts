import { create } from "zustand";
import { rpc, RpcError } from "./api";
import type { BridgeStatus, Project, Ticket } from "./types";

export type Step = "find" | "trace" | "climate" | "place" | "shape" | "units" | "program" | "diagram" | "review";

interface Toast { id: number; kind: "info" | "error" | "ok"; text: string }

interface State {
  project: Project | null;
  summary: Record<string, unknown> | null;
  tickets: Ticket[];
  bridge: BridgeStatus;
  step: Step;
  tower: string;
  unit: string | null;
  busy: string | null;
  toasts: Toast[];
  inboxOpen: boolean;
  setStep: (s: Step) => void;
  setUnit: (u: string | null) => void;
  setTower: (t: string) => void;
  setInbox: (o: boolean) => void;
  refresh: () => Promise<void>;
  refreshTickets: () => Promise<void>;
  setBridge: (b: BridgeStatus) => void;
  toast: (kind: Toast["kind"], text: string) => void;
  dismiss: (id: number) => void;
  run: <T>(label: string, fn: () => Promise<T>) => Promise<T | undefined>;
}

let toastId = 0;

export const useStore = create<State>((set, get) => ({
  project: null,
  summary: null,
  tickets: [],
  bridge: { linked: false, client: null, last_seen: 0 },
  step: "find",
  tower: "T1",
  unit: null,
  busy: null,
  toasts: [],
  inboxOpen: false,
  setStep: (step) => set({ step }),
  setUnit: (unit) => set({ unit }),
  setTower: (tower) => set({ tower }),
  setInbox: (inboxOpen) => set({ inboxOpen }),
  setBridge: (bridge) => set({ bridge }),
  refresh: async () => {
    const [project, summary] = await Promise.all([
      rpc<Project>("get_project", { full: true }),
      rpc<Record<string, unknown>>("summary"),
    ]);
    set({ project, summary });
    await get().refreshTickets();
  },
  refreshTickets: async () => {
    const tickets = await rpc<Ticket[]>("list_decisions", { status: "all" });
    set({ tickets });
  },
  toast: (kind, text) => {
    const id = ++toastId;
    set({ toasts: [...get().toasts, { id, kind, text }] });
    setTimeout(() => get().dismiss(id), kind === "error" ? 9000 : 4500);
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  run: async (label, fn) => {
    set({ busy: label });
    try {
      const out = await fn();
      await get().refresh();
      return out;
    } catch (e) {
      get().toast("error", e instanceof RpcError || e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      set({ busy: null });
    }
  },
}));

export const waiting = (t: Ticket[]) => t.filter((x) => x.status === "open" || x.status === "defaulted");
