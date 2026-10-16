import { useEffect, useState } from "react";
import { rpc } from "../lib/api";
import { useStore } from "../lib/store";

interface Status {
  app: { installed: boolean; paths: string[]; os: string }; config: string; config_exists: boolean;
  linked_in_config: boolean; error: string | null; claude_code: string; handshake: { linked: boolean; fresh?: boolean; client: string | null };
}

export default function ClaudeLink({ onClose }: { onClose: () => void }) {
  const [st, setSt] = useState<Status | null>(null);
  const [plan, setPlan] = useState<{ diff: string; path: string; already_linked: boolean; error?: string } | null>(null);
  const [result, setResult] = useState<{ message: string; backup?: string | null } | null>(null);
  const toast = useStore((s) => s.toast);
  useEffect(() => {
    void rpc<Status>("claude_status").then(setSt);
    void rpc<typeof plan>("claude_plan_link").then(setPlan);
    const t = setInterval(() => void rpc<Status>("claude_status").then(setSt), 3000);
    return () => clearInterval(t);
  }, []);
  const linked = st?.handshake.linked;
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Link Claude">
        <div className="row"><h2>Link Claude</h2><span className="spacer" />
          <span className={`badge ${linked ? "teal" : "grey"}`}><span className="dot" style={{ background: linked ? "var(--teal)" : "var(--muted)" }} />{linked ? "Linked" : "Claude not linked"}</span></div>
        <p className="muted small">We do not borrow your login. We plug the planner's tools into the Claude you already have, through MCP.</p>
        <ol className="steps">
          <li><div><b>Look.</b> {st ? (st.app.installed ? `Claude Desktop found (${st.app.os}).` : "Claude Desktop was not found on this computer.") : "Checking…"}
            <div className="mono muted">{st?.config}</div></div></li>
          <li><div><b>Ask.</b> This exact change will be made to Claude's config:
            {plan?.error ? <div className="badge red">{plan.error}</div> : <pre>{plan?.already_linked ? "Already linked - nothing to change." : plan?.diff}</pre>}</div></li>
          <li><div><b>Back up, then add.</b> The config is copied first. One entry is added. Nothing is removed.</div></li>
          <li><div><b>Restart Claude.</b> Quit Claude fully (tray / menu bar) and open it again. Or install the one-click extension (<span className="mono">apartment-planner.mcpb</span>).</div></li>
          <li><div><b>Handshake.</b> Ask Claude to “open the apartment planner”. This badge turns green when Claude starts the bridge.</div></li>
        </ol>
        {result && <div className="card soft small">{result.message}{result.backup ? <> Backup: <span className="mono">{result.backup}</span></> : null}</div>}
        <div className="row wrap">
          <button className="btn primary" disabled={!plan || !!plan.error || plan.already_linked}
            onClick={async () => {
              try {
                const r = await rpc<{ message: string; backup?: string | null }>("claude_link", { confirm: true });
                setResult(r);
                toast("ok", "Planner added to Claude. Restart Claude to finish.");
              } catch (e) { toast("error", String(e)); }
            }}>Add Apartment Planner to Claude</button>
          <button className="btn" onClick={onClose}>Close</button>
        </div>
        <details><summary className="small">Using Claude Code or your own API key?</summary>
          <p className="small">Claude Code: run <span className="mono">{st?.claude_code}</span></p>
          <p className="small muted">Without Claude every ticket keeps its safe default and is marked “check me”. The plan still finishes.</p>
        </details>
      </div>
    </div>
  );
}
