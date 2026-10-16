import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { rpc } from "../lib/api";
import { useStore } from "../lib/store";
import { Chip, Icon, LatchButton, Led, type LedStatus, Modal, ProvenanceTag, Skeleton, Spinner } from "../ui";
import "./agent.css";

interface Status {
  app: { installed: boolean; paths: string[]; os: string }; config: string; config_exists: boolean;
  linked_in_config: boolean; error: string | null; claude_code: string; handshake: { linked: boolean; fresh?: boolean; client: string | null };
}
interface Plan { diff: string; path: string; already_linked: boolean; error?: string; replacing_our_entry?: boolean }
interface LinkResult { message: string; backup?: string | null; written?: boolean; error?: string }

/** Copy text to the clipboard (with a textarea fallback for blocked clipboard APIs). Resolves true on success. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through to the legacy path */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

/** Small "Copy" latch that flips to "Copied" for a moment. `what` names the thing for screen readers. */
export function CopyButton({ text, what, label = "Copy", disabled }: { text: string; what: string; label?: string; disabled?: boolean }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const onClick = async () => {
    const ok = await copyText(text);
    setState(ok ? "ok" : "fail");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 2200);
  };
  return (
    <span className="ax-copy">
      <LatchButton size="sm" variant="ghost" disabled={disabled || !text} onClick={() => void onClick()}
        aria-label={`Copy ${what}`} icon={state === "ok" ? <Icon name="check" size={14} /> : undefined}>
        {state === "ok" ? "Copied" : state === "fail" ? "Copy failed" : label}
      </LatchButton>
      <span className="sr-only" role="status">{state === "ok" ? `${what} copied` : state === "fail" ? `Could not copy ${what}` : ""}</span>
    </span>
  );
}

type StepState = "done" | "active" | "todo" | "error" | "warn" | "loading";
const STEP_LED: Record<StepState, LedStatus> = { done: "det", active: "cobalt", todo: "idle", error: "fail", warn: "warn", loading: "cobalt" };
const STEP_WORD: Record<StepState, string> = { done: "Done", active: "Your turn", todo: "Waiting", error: "Blocked", warn: "Check", loading: "Checking" };

function Step({ n, title, state, children, word }: { n: number; title: string; state: StepState; children: ReactNode; word?: string }) {
  const cls = state === "loading" ? "active" : state === "warn" ? "done" : state;
  return (
    <li className={`ax-step is-${cls}`} aria-current={state === "active" ? "step" : undefined}>
      <div className="ax-key" aria-hidden="true">{n}<Led status={STEP_LED[state]} pulse={state === "active" || state === "loading"} /></div>
      <div className="ax-step-body">
        <div className="ax-step-head">
          <h3 className="ax-step-title">{title}</h3>
          <span className={`ax-step-status is-${cls}`}>
            {state === "loading" && <Spinner size={12} />}
            {word ?? STEP_WORD[state]}<span className="sr-only">, step {n} of 5</span>
          </span>
        </div>
        {children}
      </div>
    </li>
  );
}

/** Colour a unified diff line by line. Colour is paired with the +/- marks, never alone. */
function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <pre className="ax-diff" tabIndex={0} aria-label="Exact change to Claude's config file">
      {lines.map((l, i) => {
        const k = l.startsWith("+++") || l.startsWith("---") ? "meta" : l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
        return <code key={i} className={`ax-diff-line ${k}`}>{l || " "}</code>;
      })}
    </pre>
  );
}

const ASK_PROMPT = "Open the apartment planner";

export default function ClaudeLink({ onClose }: { onClose: () => void }) {
  const [st, setSt] = useState<Status | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [stErr, setStErr] = useState<string | null>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [result, setResult] = useState<LinkResult | null>(null);
  const [writeErr, setWriteErr] = useState<string | null>(null);
  const [writing, setWriting] = useState(false);
  const toast = useStore((s) => s.toast);
  const bridge = useStore((s) => s.bridge);

  const loadStatus = useCallback(() => rpc<Status>("claude_status").then((v) => { setSt(v); setStErr(null); }).catch((e) => setStErr(e instanceof Error ? e.message : String(e))), []);
  const loadPlan = useCallback(() => { setPlanErr(null); return rpc<Plan>("claude_plan_link").then(setPlan).catch((e) => setPlanErr(e instanceof Error ? e.message : String(e))); }, []);
  useEffect(() => {
    void loadStatus();
    void loadPlan();
    const t = setInterval(() => void loadStatus(), 3000);
    return () => clearInterval(t);
  }, [loadStatus, loadPlan]);

  const handshake = !!st?.handshake.linked || bridge.linked;
  const fresh = st ? (st.handshake.fresh ?? true) : (bridge.fresh ?? true);
  const inConfig = !!st?.linked_in_config || !!result?.written || !!plan?.already_linked;
  const planBad = !!plan?.error || !!planErr;

  const s1: StepState = stErr && !st ? "error" : !st ? "loading" : st.error ? "error" : st.app.installed ? "done" : "warn";
  const s2: StepState = planBad ? "error" : !plan ? "loading" : "done";
  const s3: StepState = planBad ? "error" : writeErr ? "error" : inConfig ? "done" : plan ? "active" : "todo";
  const s4: StepState = handshake && fresh ? "done" : inConfig ? "active" : "todo";
  const s5: StepState = handshake && fresh ? "done" : inConfig ? "active" : "todo";

  const add = async () => {
    setWriting(true); setWriteErr(null);
    try {
      const r = await rpc<LinkResult>("claude_link", { confirm: true });
      if (r.error) throw new Error(r.error);
      setResult(r);
      void loadStatus();
      toast("ok", "Planner added to Claude. Restart Claude to finish.");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setWriteErr(msg);
      toast("error", msg);
    } finally { setWriting(false); }
  };

  const statusChip = handshake && fresh
    ? <Chip led="cobalt" inset>Linked{st?.handshake.client ? ` · ${st.handshake.client}` : ""}</Chip>
    : inConfig ? <Chip led="ai">Added · waiting for Claude</Chip> : <Chip led="idle">Not linked</Chip>;

  return (
    <Modal open onClose={onClose} size="lg" className="ax-modal" eyebrow="Co-pilot · 5 steps" title="Link Claude"
      footer={<LatchButton onClick={onClose} led={false} data-autofocus>Close</LatchButton>}>
      <div className="ax-inline">
        {statusChip}
        <span className="ax-hint ax-grow">We do not borrow your login. The planner's tools plug into the Claude you already have, through MCP.</span>
      </div>

      {stErr && (
        <div className="ax-alert" role="alert">
          <Led status="fail" />
          <div className="ax-alert-body">
            <b>Cannot read Claude's status.</b> {stErr}{" "}
            <LatchButton size="sm" variant="ghost" led={false} onClick={() => void loadStatus()}>Try again</LatchButton>
          </div>
        </div>
      )}

      <ol className="ax-seq" aria-label="Link steps">
        <Step n={1} title="Look" state={s1} word={s1 === "warn" ? "Not found" : undefined}>
          {!st && !stErr && <Skeleton lines={2} />}
          {st && (
            <>
              <p className="ax-note">
                {st.error ? <>Claude's config file is not valid JSON. Fix or move it aside, then try again.</>
                  : st.app.installed ? <>Claude Desktop found on <b>{st.app.os}</b>.</>
                  : <>Claude Desktop was not found on this computer. You can still add the config below, or use Claude Code.</>}
              </p>
              <div className="ax-codebox">
                <code>{st.config}</code>
                <CopyButton text={st.config} what="config path" />
              </div>
              {st.error && <div className="ax-alert" role="alert"><Led status="fail" /><div className="ax-alert-body">{st.error}</div></div>}
            </>
          )}
        </Step>

        <Step n={2} title="Ask" state={s2} word={s2 === "done" && plan?.already_linked ? "No change" : undefined}>
          <p className="ax-note">This exact change will be made to Claude's config. Nothing else is touched.</p>
          {!plan && !planErr && <Skeleton lines={4} height={14} />}
          {(planErr || plan?.error) && (
            <div className="ax-alert" role="alert">
              <Led status="fail" />
              <div className="ax-alert-body">{plan?.error ?? planErr}{" "}
                <LatchButton size="sm" variant="ghost" led={false} onClick={() => void loadPlan()}>Try again</LatchButton></div>
            </div>
          )}
          {plan && !plan.error && (plan.already_linked
            ? <div className="ax-alert is-ok"><Led status="det" /><div className="ax-alert-body">Already linked. Nothing to change.</div></div>
            : <>
                <DiffView diff={plan.diff} />
                <div className="ax-inline">
                  <ProvenanceTag by="engine" kind="derived" source="Unified diff computed from your current config file" compact />
                  <span className="ax-hint ax-grow">One entry is added{plan.replacing_our_entry ? ", replacing our older entry" : ""}. Nothing is removed.</span>
                  <CopyButton text={plan.diff} what="config change" />
                </div>
              </>)}
        </Step>

        <Step n={3} title="Back up, then add" state={s3} word={s3 === "done" ? "Added" : undefined}>
          <p className="ax-note">The config is copied first, then one entry is added.</p>
          {result?.backup && (
            <div className="ax-codebox"><code>{result.backup}</code><CopyButton text={result.backup} what="backup path" /></div>
          )}
          {writeErr && <div className="ax-alert" role="alert"><Led status="fail" /><div className="ax-alert-body">Could not write the config. {writeErr}</div></div>}
          <div>
            <LatchButton variant="primary" loading={writing} disabled={!plan || planBad || inConfig}
              led={inConfig ? "det" : undefined} onClick={() => void add()}>
              {inConfig ? "Added to Claude" : "Add Apartment Planner to Claude"}
            </LatchButton>
          </div>
          {!plan && !planErr && <p className="ax-hint">Waiting for the change preview.</p>}
        </Step>

        <Step n={4} title="Restart Claude" state={s4}>
          <p className="ax-note">
            {inConfig ? <>Quit Claude fully (from the tray or menu bar) and open it again.</> : <>After the entry is added, quit Claude fully and open it again.</>}
            {" "}Or install the one-click extension <span className="ax-mono">apartment-planner.mcpb</span>.
          </p>
          {result?.message && <p className="ax-hint">{result.message}</p>}
        </Step>

        <Step n={5} title="Handshake" state={s5} word={s5 === "done" ? "Linked" : s5 === "active" ? "Listening" : undefined}>
          <p className="ax-note">
            {handshake && fresh ? <>Claude started the bridge{st?.handshake.client ? <> (<b>{st.handshake.client}</b>)</> : null}. Its tools are ready.</>
              : handshake ? <>Claude linked earlier but has not been seen in the last 10 minutes. Open the planner in Claude to refresh.</>
              : <>In Claude, say the line below. This step turns on when Claude starts the bridge. We check every 3 seconds.</>}
          </p>
          {!(handshake && fresh) && (
            <div className="ax-codebox"><code>“{ASK_PROMPT}”</code><CopyButton text={ASK_PROMPT} what="the test phrase" /></div>
          )}
        </Step>
      </ol>

      <details>
        <summary className="ax-note"><b>Using Claude Code or your own API key?</b></summary>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
          <p className="ax-note">Claude Code: run this once in a terminal.</p>
          <div className="ax-codebox"><code>{st?.claude_code ?? "…"}</code><CopyButton text={st?.claude_code ?? ""} what="Claude Code command" disabled={!st} /></div>
          <p className="ax-hint">Without Claude, every decision keeps its safe default and is marked <i>check me</i>. The plan still finishes.</p>
        </div>
      </details>
    </Modal>
  );
}
