// Keyboard shortcut reference overlay (press ?). Lists only keys that are actually bound.
import type { ReactNode } from "react";
import { Kbd, LatchButton, Modal } from "../ui";
import { modKey } from "./storage";
import "./flows.css";

interface Row { keys: ReactNode; does: string }
interface Group { title: string; note?: string; rows: Row[] }

const sep = <span className="fx-keysep" aria-hidden="true">/</span>;

function groups(): Group[] {
  const mod = modKey();
  return [
    {
      title: "Everywhere",
      rows: [
        { keys: <><Kbd>{mod}</Kbd><Kbd>K</Kbd></>, does: "Open the command palette" },
        { keys: <Kbd>?</Kbd>, does: "Show this list" },
        { keys: <Kbd>Esc</Kbd>, does: "Close a dialog or the palette" },
      ],
    },
    {
      title: "Stages",
      note: "Locked stages explain why instead of opening.",
      rows: [
        { keys: <><Kbd>1</Kbd>{sep}<Kbd>8</Kbd></>, does: "Jump to a stage" },
        { keys: <><Kbd>[</Kbd>{sep}<Kbd>]</Kbd></>, does: "Previous or next open stage" },
        { keys: <><Kbd>←</Kbd><Kbd>→</Kbd></>, does: "Move between stage keys (then Enter to open)" },
      ],
    },
    {
      title: "Find and trace",
      rows: [
        { keys: <Kbd>/</Kbd>, does: "Jump to the place search (stage 1)" },
        { keys: <><Kbd>{mod}</Kbd><Kbd>Z</Kbd></>, does: "Undo the last corner (stage 2) or link (stage 7)" },
        { keys: <><Kbd>⇧</Kbd><Kbd>{mod}</Kbd><Kbd>Z</Kbd></>, does: "Put it back" },
      ],
    },
    {
      title: "Decision inbox",
      note: "Active while the inbox is open.",
      rows: [
        { keys: <><Kbd>J</Kbd>{sep}<Kbd>K</Kbd></>, does: "Next or previous ticket" },
        { keys: <Kbd>Enter</Kbd>, does: "Keep the selected ticket" },
        { keys: <Kbd>U</Kbd>, does: "Undo it and return to the safe default" },
      ],
    },
  ];
}

export function Shortcuts({ open, onClose, onOpenPalette }: { open: boolean; onClose: () => void; onOpenPalette: () => void }) {
  return (
    <Modal open={open} onClose={onClose} eyebrow="Help" title="Keyboard shortcuts" size="md"
      footer={
        <>
          <LatchButton variant="ghost" onClick={() => { onClose(); setTimeout(onOpenPalette, 0); }}>Open commands</LatchButton>
          <LatchButton variant="primary" data-autofocus onClick={onClose}>Done</LatchButton>
        </>
      }>
      <p className="fx-help-intro">Shortcuts pause while you type in a field or a dialog is open.</p>
      <div className="fx-help-grid">
        {groups().map((g) => (
          <section key={g.title} className="fx-help-group neo-well neo-sm" aria-label={g.title}>
            <h3 className="fx-help-h">{g.title}</h3>
            <dl className="fx-help-list">
              {g.rows.map((r, i) => (
                <div key={i} className="fx-help-row">
                  <dt className="fx-help-keys">{r.keys}</dt>
                  <dd className="fx-help-does">{r.does}</dd>
                </div>
              ))}
            </dl>
            {g.note && <p className="fx-help-note">{g.note}</p>}
          </section>
        ))}
      </div>
    </Modal>
  );
}
