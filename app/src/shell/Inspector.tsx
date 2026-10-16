import type { Project, Ticket } from "../lib/types";
import { Chip, Icon, IconButton, LatchButton, Led, Panel, ProvenanceTag, Readout, StatusChip } from "../ui";
import type { StageMeta } from "./stages";

export interface InspectorProps {
  stage: StageMeta;
  project: Project | null;
  summary: Record<string, unknown> | null;
  waiting: Ticket[];
  claudeLinked: boolean;
  onOpenInbox: () => void;
  onClose: () => void;
}

/**
 * Right dock (default content). Shows the stage contract (what the rules do vs what Claude may do),
 * decisions waiting, and project telemetry with provenance. Stage agents may add sections here.
 */
export function Inspector({ stage, project, summary, waiting, claudeLinked, onOpenInbox, onClose }: InspectorProps) {
  const site = project?.site;
  const atStage = waiting.filter((t) => t.stage === stage.n).length;
  const plates = project?.plates ?? [];
  const plateArea = plates.length ? plates.reduce((a, p) => a + p.area, 0) : null;
  const counts = project?.checks?.counts;
  const carpet = typeof summary?.carpet_m2 === "number" ? (summary.carpet_m2 as number) : null;
  return (
    <div className="inspector">
      <div className="dock-head">
        <div className="eyebrow">Inspector</div>
        <IconButton label="Hide inspector" size="sm" icon={<Icon name="close" />} onClick={onClose} />
      </div>

      <Panel profile="well" pad="sm" title="Stage contract" eyebrow={`${String(stage.n).padStart(2, "0")} · ${stage.title}`}>
        <div className="contract-row">
          <ProvenanceTag by="engine" compact />
          <p className="contract-text">{stage.engine}</p>
        </div>
        <div className="contract-row">
          <ProvenanceTag by="claude" compact kind={stage.copilot ? undefined : "unknown"} />
          <p className={stage.copilot ? "contract-text backlit-text" : "contract-text muted"}>
            {stage.copilot ?? "No co-pilot role. 100% deterministic."}
          </p>
        </div>
      </Panel>

      <Panel profile="flat" pad="sm" small title="Decisions" actions={<Led status={waiting.length ? "ai" : "idle"} pulse={waiting.length > 0} label={waiting.length ? "Decisions waiting" : "No decisions waiting"} />}>
        <p className="small">
          {waiting.length === 0 ? "Nothing waiting. Every choice so far is settled."
            : <><b>{waiting.length}</b> waiting{atStage ? <>, <b>{atStage}</b> from this stage</> : null}. Each already has a safe default applied, marked <i>check me</i>.</>}
        </p>
        <p className="small muted">{claudeLinked ? "Claude is linked and can answer them; answers are re-checked by the rules." : "Link Claude to have it answer them, or decide yourself."}</p>
        <LatchButton variant="ai" size="sm" block onClick={onOpenInbox} pulse={waiting.length > 0}>Open decision inbox</LatchButton>
      </Panel>

      <Panel profile="flat" pad="sm" small title="Telemetry">
        <div className="inspector-grid">
          <Readout label="Site" value={site?.area} unit="m²" tone="det" size="sm" layout="inline" by="engine" kind="derived" source="Shoelace area in the local UTM grid" />
          <Readout label="Buildable" value={site?.buildable_area} unit="m²" tone="det" size="sm" layout="inline" by="engine" kind="derived" source={site?.setback_clause} />
          <Readout label="Setback" value={site?.setback} unit="m" tone="det" size="sm" layout="inline" by="engine" kind="derived" source={site?.setback_clause} />
          <Readout label="Height" value={site?.building_height} unit="m" tone="ink" size="sm" layout="inline" by="user" kind="observed" />
          <Readout label={plates.length > 1 ? `Plates (${plates.length})` : "Plate"} value={plateArea} unit="m²" tone="det" size="sm" layout="inline" by="engine" kind="derived" />
          <Readout label="Flats" value={project?.units.length || null} digits={0} tone="ink" size="sm" layout="inline" by="engine" kind="derived" />
          <Readout label="Carpet" value={carpet} unit="m²" tone="det" size="sm" layout="inline" by="engine" kind="derived" />
          <Readout label="Plans" value={project?.plans.length || null} digits={0} tone="ink" size="sm" layout="inline" by="engine" kind="derived" />
        </div>
        {counts && (
          <div className="row wrap">
            <StatusChip status="pass" count={counts.pass ?? 0} />
            <StatusChip status="review" count={counts.review ?? 0} />
            <StatusChip status="fail" count={counts.fail ?? 0} />
          </div>
        )}
      </Panel>

      <details className="inspector-legend">
        <summary>How to read provenance</summary>
        <ul>
          <li><Chip led="det" inset>Rule</Chip> engraved: computed by the rule engine, exact and repeatable.</li>
          <li><Chip led="ai">Co-pilot</Chip> backlit: suggested by Claude, always re-checked by the rules.</li>
          <li><Chip led="cobalt">User</Chip> typed or clicked by you.</li>
          <li><b>Observed</b> measured from an input · <b>Derived</b> computed from observed data · <b>Inferred</b> chosen by Claude · <b>Unknown</b> shown as “–”, never guessed.</li>
        </ul>
      </details>
    </div>
  );
}
