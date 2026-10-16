import { useEffect, useState } from "react";
import { rpc } from "../lib/api";
import { useStore } from "../lib/store";

export interface AppSettings { mapbox_token: string; map_provider: string; imagery: string; geocoder: string; crash_reports: boolean }

export default function Settings({ onClose, onSaved }: { onClose: () => void; onSaved: (s: AppSettings) => void }) {
  const [s, setS] = useState<AppSettings | null>(null);
  const [token, setToken] = useState("");
  const [show, setShow] = useState(false);
  const toast = useStore((x) => x.toast);
  useEffect(() => { void rpc<AppSettings>("get_settings").then((v) => { setS(v); setToken(v.mapbox_token); }); }, []);
  if (!s) return null;
  const save = async () => {
    try {
      const v = await rpc<AppSettings>("set_settings", { mapbox_token: token.trim(), map_provider: s.map_provider, crash_reports: s.crash_reports });
      onSaved(v);
      toast("ok", v.imagery === "mapbox" ? "Saved. Map and search now use Mapbox." : "Saved.");
      onClose();
    } catch (e) { toast("error", e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Settings">
        <h2>Settings</h2>
        <div className="col">
          <h3>Map and place search</h3>
          <p className="small muted">Paste a free public Mapbox token (starts with <span className="mono">pk.</span>) for Mapbox satellite imagery
            with street names and Mapbox place search. The free tier covers normal use; results are cached on this computer.
            Create one at <a href="https://account.mapbox.com/access-tokens/" target="_blank" rel="noreferrer">account.mapbox.com</a>.
            Without a token the app uses Esri imagery and OpenStreetMap search.</p>
          <label className="field" htmlFor="mbx">Mapbox token
            <div className="row">
              <input id="mbx" style={{ flex: 1 }} type={show ? "text" : "password"} value={token} placeholder="pk.eyJ1Ijoi…"
                onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} />
              <button className="btn small" type="button" onClick={() => setShow(!show)}>{show ? "Hide" : "Show"}</button>
            </div>
          </label>
          <label className="field" htmlFor="prov">Satellite imagery
            <select id="prov" value={s.map_provider} onChange={(e) => setS({ ...s, map_provider: e.target.value })}>
              <option value="auto">Mapbox when a token is set, else Esri</option>
              <option value="mapbox">Mapbox</option>
              <option value="esri">Esri World Imagery</option>
            </select>
          </label>
          <label className="row small" htmlFor="crash">
            <input id="crash" type="checkbox" checked={s.crash_reports} onChange={(e) => setS({ ...s, crash_reports: e.target.checked })} />
            Send anonymous crash reports (stack traces only, no project data)
          </label>
          <div className="small muted">Now: imagery from <b>{s.imagery}</b>, search with <b>{s.geocoder}</b>.</div>
        </div>
        <div className="row"><button className="btn primary" onClick={save}>Save</button><button className="btn" onClick={onClose}>Cancel</button></div>
      </div>
    </div>
  );
}
