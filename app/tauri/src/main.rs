#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

fn find_bfagent() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // Sibling next to the Tauri binary (production layout)
            let candidate = dir.join("bfagent");
            if candidate.exists() {
                return candidate;
            }
            // dev layout: tauri/target/release/bfagent-app -> ../../bfagent
            let dev = dir.join("../../bfagent");
            if dev.exists() {
                return dev;
            }
            let dev2 = dir.join("../../../bfagent");
            if dev2.exists() {
                return dev2;
            }
        }
    }
    PathBuf::from("bfagent")
}

fn port_open(host: &str, port: u16) -> bool {
    if let Ok(addr) = format!("{}:{}", host, port).parse() {
        TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok()
    } else {
        false
    }
}

fn main() {
    let bfagent_path = find_bfagent();
    eprintln!("[bfagent-app] launching {}", bfagent_path.display());

    let mut child = Command::new(&bfagent_path)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("failed to start bfagent");

    let stdout = child.stdout.take().expect("no stdout");
    let stderr = child.stderr.take().expect("no stderr");

    let url_holder: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let url_writer = url_holder.clone();

    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            println!("[bfagent] {}", line);
            if let Some(rest) = line.strip_prefix("[bfagent] ui-url=") {
                let u = rest.trim().to_string();
                *url_writer.lock().unwrap() = Some(u);
            }
        }
    });

    thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            eprintln!("[bfagent] {}", line);
        }
    });

    let child_handle = Arc::new(Mutex::new(Some(child)));
    let child_for_close = child_handle.clone();
    let url_for_setup = url_holder.clone();

    tauri::Builder::default()
        .setup(move |app| {
            let app_handle = app.handle().clone();
            let icon: Option<tauri::image::Image<'static>> =
                app.default_window_icon().map(|i| i.clone().to_owned());
            let url_holder = url_for_setup.clone();
            thread::spawn(move || {
                // wait until program.py prints the URL
                let url = loop {
                    if let Some(u) = url_holder.lock().unwrap().clone() {
                        break u;
                    }
                    thread::sleep(Duration::from_millis(150));
                };

                // parse to extract host/port for TCP probe
                let parsed: tauri::Url = match url.parse() {
                    Ok(u) => u,
                    Err(e) => {
                        eprintln!("[bfagent-app] bad url {}: {}", url, e);
                        return;
                    }
                };
                let host = parsed.host_str().unwrap_or("127.0.0.1").to_string();
                let port = parsed.port().unwrap_or(80);

                // wait for the UI port to start accepting connections
                let deadline = Instant::now() + Duration::from_secs(900);
                while Instant::now() < deadline {
                    if port_open(&host, port) {
                        break;
                    }
                    thread::sleep(Duration::from_millis(400));
                }
                // small grace period for Gradio to finish bootstrapping
                thread::sleep(Duration::from_millis(800));

                // open the native window pointing at the live UI
                let app_handle_for_main = app_handle.clone();
                let icon_for_main = icon.clone();
                let _ = app_handle.run_on_main_thread(move || {
                    let mut builder = WebviewWindowBuilder::new(
                        &app_handle_for_main,
                        "main",
                        WebviewUrl::External(parsed),
                    )
                    .title("bfagent")
                    .inner_size(1280.0, 820.0)
                    .min_inner_size(720.0, 520.0)
                    .resizable(true);
                    if let Some(ic) = icon_for_main {
                        builder = builder
                            .icon(ic)
                            .expect("icon set on already-decoded image");
                    }
                    if let Err(e) = builder.build() {
                        eprintln!("[bfagent-app] window build failed: {}", e);
                    }
                });
            });
            Ok(())
        })
        .on_window_event(move |_window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                if let Some(mut c) = child_for_close.lock().unwrap().take() {
                    let _ = c.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("tauri app run failed");

    // belt-and-suspenders: if the event loop exits, make sure bfagent is gone
    let leftover = child_handle.lock().unwrap().take();
    if let Some(mut c) = leftover {
        let _ = c.kill();
    }
}
