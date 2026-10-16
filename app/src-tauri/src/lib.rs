//! Desktop shell: starts the Python engine as a sidecar (`planner-engine serve`), waits for its
//! "listening" line, then points the window at the engine's local UI. The engine is stopped when the
//! app quits. Claude Desktop starts the same binary separately (`planner-engine mcp`) and finds the
//! running app through ~/.apartment-planner/server.json.

use std::sync::Mutex;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct Engine(Mutex<Option<CommandChild>>);

#[cfg(feature = "updater")]
mod updater {
    pub fn register<R: tauri::Runtime>(b: tauri::Builder<R>) -> tauri::Builder<R> {
        b.plugin(tauri_plugin_updater::Builder::new().build())
    }
}

fn listening_url(line: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    if v.get("event")?.as_str()? == "listening" {
        return v.get("url")?.as_str().map(|s| s.to_string());
    }
    None
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_shell::init());
    #[cfg(feature = "updater")]
    let builder = updater::register(builder);

    let app = builder
        .setup(|app| {
            let cmd = app
                .shell()
                .sidecar("planner-engine")
                .map_err(|e| format!("engine sidecar missing: {e}"))?
                .args(["serve", "--no-browser", "--parent-pid", &std::process::id().to_string()]);
            let (mut rx, child) = cmd.spawn().map_err(|e| format!("could not start the engine: {e}"))?;
            app.manage(Engine(Mutex::new(Some(child))));
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut opened = false;
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) => {
                            let text = String::from_utf8_lossy(&bytes).to_string();
                            for line in text.lines() {
                                if let Some(url) = listening_url(line) {
                                    if let (Some(win), Ok(parsed)) = (handle.get_webview_window("main"), url.parse()) {
                                        let _ = win.navigate(parsed);
                                        opened = true;
                                    }
                                }
                            }
                        }
                        CommandEvent::Stderr(bytes) => eprintln!("[engine] {}", String::from_utf8_lossy(&bytes)),
                        CommandEvent::Terminated(status) => {
                            eprintln!("[engine] exited: {:?}", status.code);
                            if let Some(win) = handle.get_webview_window("main") {
                                let msg = if opened { "The planner engine stopped." } else { "The planner engine could not start." };
                                let _ = win.eval(&format!(
                                    "document.body.innerHTML='<div style=\"font:15px system-ui;padding:40px\"><h2>{msg}</h2><p>Run <code>planner-engine doctor</code> from a terminal for details, then restart the app.</p></div>'"
                                ));
                            }
                            break;
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Apartment Planner app");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(engine) = handle.try_state::<Engine>() {
                if let Some(child) = engine.0.lock().ok().and_then(|mut c| c.take()) {
                    let _ = child.kill();
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    #[test]
    fn parses_listening_line() {
        let l = r#"{"event": "listening", "port": 8765, "url": "http://127.0.0.1:8765"}"#;
        assert_eq!(super::listening_url(l).as_deref(), Some("http://127.0.0.1:8765"));
        assert_eq!(super::listening_url("hello"), None);
    }
}
