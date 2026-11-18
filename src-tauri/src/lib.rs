//! Rank Checker desktop app.
//!
//! A thin window around the Python dashboard (`app.py`). On launch it starts the
//! bundled server (`resources/server/rank-checker-server.exe`, built with PyInstaller)
//! in `--app` mode, waits for the `RANK_CHECKER_URL <url>` line it prints, then points
//! the window at that address. Results live in the app data folder
//! (`%APPDATA%\uk.co.paramountdigital.rank-checker\results`). The server stops when the
//! app closes its stdin.
//!
//! Dev builds (`npm run tauri dev`) run `app.py` from the repo with `.venv`'s Python.

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

#[derive(Default)]
struct Server(Mutex<Option<Child>>);

/// Hosts the dashboard is allowed to load in the window. Anything else opens in the browser.
fn is_local(url: &Url) -> bool {
    matches!(url.scheme(), "tauri" | "about" | "data" | "blob")
        || matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "tauri.localhost"))
}

fn server_command(app: &AppHandle) -> Result<Command, String> {
    let mut cmd = if cfg!(debug_assertions) {
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
        let venv = repo.join(".venv").join("Scripts").join("python.exe");
        let mut c = Command::new(if venv.exists() { venv } else { PathBuf::from("python") });
        c.arg(repo.join("app.py")).current_dir(&repo);
        c
    } else {
        let dir = app.path().resource_dir().map_err(|e| e.to_string())?.join("server");
        let exe = dir.join(if cfg!(windows) { "rank-checker-server.exe" } else { "rank-checker-server" });
        if !exe.exists() {
            return Err(format!("The bundled server is missing: {}", exe.display()));
        }
        let mut c = Command::new(exe);
        c.current_dir(dir);
        c
    };
    cmd.arg("--app")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Dev runs can point at a scratch folder; release builds always use app data.
    if cfg!(not(debug_assertions)) || std::env::var_os("RANK_CHECKER_DATA_DIR").is_none() {
        let data = app.path().app_data_dir().map_err(|e| e.to_string())?.join("results");
        cmd.env("RANK_CHECKER_DATA_DIR", data);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    Ok(cmd)
}

fn show_error(app: &AppHandle, message: &str, detail: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let js = format!(
            "window.showError({}, {})",
            serde_json::to_string(message).unwrap_or_default(),
            serde_json::to_string(detail).unwrap_or_default()
        );
        let _ = w.eval(&js);
    }
}

/// Starts the server, logs its output to `server.log` in app data, and loads the
/// dashboard once the server says where it is listening.
fn start_server(app: AppHandle) {
    let log_path = app
        .path()
        .app_log_dir()
        .map(|d| {
            let _ = std::fs::create_dir_all(&d);
            d.join("server.log")
        })
        .ok();
    let log = log_path.as_ref().and_then(|p| File::create(p).ok());
    let log = std::sync::Arc::new(Mutex::new(log));
    let write_log = {
        let log = log.clone();
        move |line: &str| {
            if let Some(f) = log.lock().unwrap().as_mut() {
                let _ = writeln!(f, "{line}");
            }
        }
    };

    let mut child = match server_command(&app).and_then(|mut c| c.spawn().map_err(|e| e.to_string())) {
        Ok(c) => c,
        Err(e) => {
            write_log(&format!("Could not start the server: {e}"));
            show_error(&app, "The rank checking service didn't start.", &e);
            return;
        }
    };
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    *app.state::<Server>().0.lock().unwrap() = Some(child);

    // stderr: keep the last lines for the error screen.
    let tail = std::sync::Arc::new(Mutex::new(Vec::<String>::new()));
    if let Some(stderr) = stderr {
        let (tail, write_log) = (tail.clone(), write_log.clone());
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                write_log(&line);
                let mut t = tail.lock().unwrap();
                t.push(line);
                if t.len() > 15 {
                    t.remove(0);
                }
            }
        });
    }

    let Some(stdout) = stdout else { return };
    std::thread::spawn(move || {
        let mut loaded = false;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            write_log(&line);
            if loaded {
                continue;
            }
            if let Some(url) = line.strip_prefix("RANK_CHECKER_URL ") {
                if let (Ok(url), Some(w)) = (Url::parse(url.trim()), app.get_webview_window("main")) {
                    let _ = w.navigate(url);
                    loaded = true;
                    check_for_update(app.clone());
                }
            }
        }
        // stdout closed: the server has exited.
        std::thread::sleep(Duration::from_millis(300));
        let detail = tail.lock().unwrap().join("\n");
        let where_log = log_path
            .map(|p| format!(" Details are in {}.", p.display()))
            .unwrap_or_default();
        if loaded {
            let _ = app
                .dialog()
                .message(format!("The rank checking service stopped unexpectedly. Close and reopen Rank Checker.{where_log}"))
                .title("Rank Checker")
                .kind(MessageDialogKind::Error)
                .show(|_| {});
        } else {
            show_error(&app, &format!("The rank checking service stopped while starting.{where_log}"), &detail);
        }
    });
}

/// Closes the server's stdin (it exits on its own), then makes sure it is gone.
fn stop_server(app: &AppHandle) {
    let Some(mut child) = app.state::<Server>().0.lock().unwrap().take() else { return };
    drop(child.stdin.take());
    for _ in 0..20 {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// Offers a newer release, if there is one. Silent when offline or up to date.
fn check_for_update(app: AppHandle) {
    #[cfg(desktop)]
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_updater::UpdaterExt;
        let Ok(updater) = app.updater() else { return };
        let Ok(Some(update)) = updater.check().await else { return };
        let version = update.version.clone();
        let handle = app.clone();
        app.dialog()
            .message(format!(
                "Rank Checker {version} is available (you have {}).\n\nInstalling closes the app, so any check that is running will stop.",
                update.current_version
            ))
            .title("Update available")
            .buttons(MessageDialogButtons::OkCancelCustom("Install and restart".into(), "Later".into()))
            .show(move |install| {
                if !install {
                    return;
                }
                tauri::async_runtime::spawn(async move {
                    match update.download_and_install(|_, _| {}, || {}).await {
                        Ok(()) => {
                            stop_server(&handle);
                            handle.restart();
                        }
                        Err(e) => {
                            let _ = handle
                                .dialog()
                                .message(format!("The update didn't install: {e}"))
                                .title("Update failed")
                                .kind(MessageDialogKind::Error)
                                .show(|_| {});
                        }
                    }
                });
            });
    });
}

fn build_window(app: &AppHandle) -> tauri::Result<()> {
    let opener = app.clone();
    let opener2 = app.clone();
    let revealer = app.clone();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Rank Checker")
        .inner_size(1360.0, 900.0)
        .min_inner_size(900.0, 600.0)
        // Lets the dashboard's own drop zone receive Wincher exports.
        .disable_drag_drop_handler()
        .on_navigation(move |url| {
            if is_local(url) {
                return true;
            }
            let _ = opener.opener().open_url(url.as_str(), None::<&str>);
            false
        })
        .on_new_window(move |url, _| {
            let _ = opener2.opener().open_url(url.as_str(), None::<&str>);
            NewWindowResponse::Deny
        })
        .on_download(move |_, event| {
            // Exports go to Downloads as usual; show the file once it lands.
            if let DownloadEvent::Finished { path: Some(path), success: true, .. } = event {
                let _ = revealer.opener().reveal_item_in_dir(path);
            }
            true
        })
        .build()?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();

    #[cfg(desktop)]
    {
        builder = builder
            // Must be registered first: a second launch focuses the open window instead.
            .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.unminimize();
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }))
            // Remembers window size, position and maximised state.
            .plugin(tauri_plugin_window_state::Builder::default().build())
            .plugin(tauri_plugin_updater::Builder::new().build())
            .plugin(tauri_plugin_process::init());
    }

    builder
        .manage(Server::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            build_window(app.handle())?;
            start_server(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while starting Rank Checker")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_server(app);
            }
        });
}
