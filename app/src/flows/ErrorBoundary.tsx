import { Component, type ErrorInfo, type ReactNode } from "react";
import { useStore } from "../lib/store";
import { Icon, LatchButton, Logo } from "../ui";
import "./flows.css";

interface Props { children: ReactNode }
interface State { error: Error | null; info: string; copied: boolean }

/**
 * Catches render errors anywhere below it and shows a calm recovery screen instead of a blank page.
 * Recovery ladder: Try again (re-render), Start from stage 1 (reset the step, then re-render), Reload.
 * Project data lives in the engine, so none of these touch it.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: "", copied: false };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error, copied: false };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ info: (info.componentStack ?? "").trim() });
    console.error("[ivai] screen crashed:", error, info.componentStack);
  }

  /** Re-read the project from the engine first (bad local state is the usual cause), then re-render. */
  private reset = () => {
    const done = () => this.setState({ error: null, info: "", copied: false });
    try { useStore.getState().refresh().then(done, done); } catch { done(); }
  };

  private restart = () => {
    try { useStore.getState().setStep("find"); } catch { /* store unavailable: reset anyway */ }
    this.reset();
  };

  private copy = async () => {
    const { error, info } = this.state;
    const text = `Apartment Planner error\n${error?.name ?? "Error"}: ${error?.message ?? ""}\n${error?.stack ?? ""}\n${info}`;
    try {
      await navigator.clipboard.writeText(text);
      this.setState({ copied: true });
    } catch { /* clipboard blocked: details stay selectable below */ }
  };

  render() {
    const { error, info, copied } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="fx-crash" role="alert">
        <div className="fx-crash-slab neo-flat">
          <Logo variant="tile" size={36} wordmark />
          <div className="fx-crash-icon neo-well" aria-hidden="true"><Icon name="alert" size={26} /></div>
          <h1 className="fx-crash-title">This screen hit a snag</h1>
          <p className="fx-crash-body">
            Something unexpected broke while drawing the page. Your project is stored by the engine and is not touched by this.
          </p>
          <div className="fx-crash-actions">
            <LatchButton variant="primary" data-autofocus autoFocus onClick={this.reset}>Try again</LatchButton>
            <LatchButton onClick={this.restart}>Start from stage 1</LatchButton>
            <LatchButton variant="ghost" onClick={() => window.location.reload()}>Reload the app</LatchButton>
          </div>
          <details className="fx-crash-details neo-well neo-sm">
            <summary>Technical details</summary>
            <pre className="fx-crash-pre">{`${error.name}: ${error.message}${info ? `\n\n${info}` : ""}`}</pre>
            <LatchButton size="sm" onClick={() => void this.copy()} icon={<Icon name={copied ? "check" : "download"} size={14} />}>
              {copied ? "Copied" : "Copy details"}
            </LatchButton>
          </details>
        </div>
      </div>
    );
  }
}
