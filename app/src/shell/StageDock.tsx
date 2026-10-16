import type { ReactNode } from "react";
import type { Step } from "../lib/store";
import { Tabs } from "../ui";
import type { StageMeta } from "./stages";

export interface StageDockProps {
  stage: StageMeta;
  step: Step;
  onStep: (s: Step) => void;
  /** Stage 6 sub-step availability (program needs flats - same gate as units). */
  children: ReactNode;
}

const SUB_LABEL: Partial<Record<Step, string>> = { units: "Split flats", program: "Room lists" };

/** Left dock chrome: engraved stage header + (for stage 6) the Split / Room lists switch, then the stage panel. */
export function StageDock({ stage, step, onStep, children }: StageDockProps) {
  return (
    <>
      <div className="dock-head">
        <div className="eyebrow">Stage {String(stage.n).padStart(2, "0")} / 08 · {stage.label}</div>
        <div className="dock-share" aria-label={`Rules ${stage.rules}%, co-pilot ${100 - stage.rules}%`}>
          <span className="dock-share-bar" aria-hidden="true">
            <span className="seq-share-det" style={{ width: `${stage.rules}%` }} />
            <span className="seq-share-ai" style={{ width: `${100 - stage.rules}%` }} />
          </span>
          <span className="dock-share-text" aria-hidden="true">{stage.rules}/{100 - stage.rules}</span>
        </div>
      </div>
      {stage.steps.length > 1 && (
        <Tabs label={`${stage.label} steps`} size="sm" block value={step}
          items={stage.steps.map((s) => ({ id: s, label: SUB_LABEL[s] ?? s }))} onChange={onStep} />
      )}
      <div className="dock-body" id="stage-panel-body">{children}</div>
    </>
  );
}
