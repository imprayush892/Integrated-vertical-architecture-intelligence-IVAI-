import { EmptyState, Icon, LatchButton, Logo } from "../ui";

/** Full-screen state when the local engine cannot be reached at boot. */
export function EngineOffline({ error }: { error: string }) {
  return (
    <div className="offline">
      <div className="offline-slab neo-flat">
        <Logo variant="tile" size={36} wordmark />
        <EmptyState icon={<Icon name="alert" size={26} />} title="The planner engine is not running"
          action={<LatchButton variant="primary" onClick={() => window.location.reload()}>Retry</LatchButton>}>
          <p className="mono small" style={{ overflowWrap: "anywhere" }}>{error}</p>
          <p>Start it with <span className="mono">planner serve</span>, or reopen the desktop app.</p>
        </EmptyState>
      </div>
    </div>
  );
}
