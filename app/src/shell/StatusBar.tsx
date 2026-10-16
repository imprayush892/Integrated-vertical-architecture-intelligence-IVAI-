import { Led, Progress } from "../ui";
import type { EngineState } from "./useEngineHealth";

export const RULESET_LABEL = "DCPR 2034.1 · NBC 2016";
export const DISCLAIMER = "Automated pre-check. Confirm with the project's licensed consultant.";

export interface StatusBarProps {
  engine: EngineState;
  engineVersion: string | null;
  /** Current long-running action label from the store (null = idle). */
  busy: string | null;
  crs: string;
}

/** Bottom telemetry strip: engine link, solver, units, CRS, rule set, disclaimer. */
export function StatusBar({ engine, engineVersion, busy, crs }: StatusBarProps) {
  const engLed = engine === "online" ? "det" : engine === "offline" ? "fail" : "idle";
  return (
    <footer className="statusbar" aria-label="Status">
      <div className="statusbar-strip">
        <span className="sb-item" title="Local planner engine">
          <Led status={engLed} pulse={engine === "checking"} />
          <span>Engine {engine === "online" ? "online" : engine === "offline" ? "offline" : "connecting"}{engineVersion ? ` · v${engineVersion}` : ""}</span>
        </span>
        <span className="sb-item sb-solver" aria-live="polite">
          {busy ? (
            <>
              <Led status="det" pulse />
              <span className="sb-busy-text">{busy}…</span>
              <Progress label={busy} className="sb-progress" />
            </>
          ) : (<><Led status="idle" /><span>Solver idle</span></>)}
        </span>
        <span className="sb-item sb-hide-md"><span className="sb-k">UNITS</span> m</span>
        <span className="sb-item sb-hide-md"><span className="sb-k">CRS</span> {crs}</span>
        <span className="sb-item sb-hide-sm"><span className="sb-k">RULES</span> {RULESET_LABEL}</span>
        <span className="spacer" />
        <span className="sb-item sb-disclaimer">{DISCLAIMER}</span>
      </div>
    </footer>
  );
}
