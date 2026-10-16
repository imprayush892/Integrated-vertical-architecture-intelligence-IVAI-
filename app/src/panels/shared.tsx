// Small helpers shared by the stage panels (moved verbatim from Panels.tsx).

export const Kpi = ({ label, value }: { label: string; value: string }) => (
  <div className="kpi"><span>{label}</span><b>{value}</b></div>
);
