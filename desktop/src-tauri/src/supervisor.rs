// GUI process lifecycle only. Tenant permissions and business logic live in Python.
use serde::{Deserialize, Serialize};
use std::{
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::Emitter;

#[derive(Clone, Serialize)]
pub struct Info {
    url: String,
    token: String,
}
#[derive(Deserialize)]
struct Ready {
    port: u16,
    protocol: u8,
}
#[derive(Default)]
struct Inner {
    stopping: AtomicBool,
    child: Mutex<Option<Child>>,
    info: Mutex<Option<Info>>,
}
#[derive(Default, Clone)]
pub struct Supervisor(Arc<Inner>);

fn executable() -> PathBuf {
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join(format!(
                "platform-sidecar-{}{}",
                env!("PLATFORM_TARGET"),
                suffix
            ))
    } else {
        std::env::current_exe()
            .expect("Application path unavailable")
            .parent()
            .unwrap()
            .join(format!("platform-sidecar{}", suffix))
    }
}

impl Supervisor {
    pub fn info(&self) -> Result<Info, String> {
        self.0
            .info
            .lock()
            .unwrap()
            .clone()
            .ok_or_else(|| "Local backend is starting".into())
    }

    fn terminate_child(&self) {
        if let Some(mut child) = self.0.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *self.0.info.lock().unwrap() = None;
    }

    pub fn stop(&self) {
        self.0.stopping.store(true, Ordering::SeqCst);
        self.terminate_child();
    }

    pub fn start(&self, app: tauri::AppHandle) {
        let supervisor = self.clone();
        thread::spawn(move || {
            let token = format!(
                "{}{}",
                uuid::Uuid::new_v4().simple(),
                uuid::Uuid::new_v4().simple()
            );
            let upstream = std::env::var("PLATFORM_UPSTREAM")
                .unwrap_or_else(|_| "https://platform.k3s.local".into());
            let mut backoff = 1u64;
            while !supervisor.0.stopping.load(Ordering::SeqCst) {
                let started = Instant::now();
                let mut command = Command::new(executable());
                command
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null());
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    command.creation_flags(0x08000000); // CREATE_NO_WINDOW
                }
                if let Ok(mut child) = command.spawn() {
                    let stdout = child.stdout.take().unwrap();
                    let mut origins = vec!["http://tauri.localhost", "tauri://localhost"];
                    if cfg!(debug_assertions) {
                        origins.push("http://localhost:5173");
                    }
                    let handshake = serde_json::json!({"token": token, "upstream": upstream, "origins": origins});
                    let sent = writeln!(child.stdin.as_mut().unwrap(), "{}", handshake)
                        .and_then(|_| child.stdin.as_mut().unwrap().flush())
                        .is_ok();
                    *supervisor.0.child.lock().unwrap() = Some(child);
                    let (tx, rx) = std::sync::mpsc::channel();
                    thread::spawn(move || {
                        let mut line = String::new();
                        if BufReader::new(stdout).read_line(&mut line).is_ok() {
                            let _ = tx.send(line);
                        }
                    });
                    let ready = if sent {
                        rx.recv_timeout(Duration::from_secs(15))
                            .ok()
                            .and_then(|line| serde_json::from_str::<Ready>(&line).ok())
                    } else {
                        None
                    };
                    if let Some(Ready { port, protocol: 1 }) = ready.filter(|ready| ready.port != 0)
                    {
                        if !supervisor.0.stopping.load(Ordering::SeqCst) {
                            *supervisor.0.info.lock().unwrap() = Some(Info {
                                url: format!("http://127.0.0.1:{}", port),
                                token: token.clone(),
                            });
                            let _ = app.emit("sidecar-ready", ());
                        }
                        while !supervisor.0.stopping.load(Ordering::SeqCst) {
                            let alive = supervisor
                                .0
                                .child
                                .lock()
                                .unwrap()
                                .as_mut()
                                .map(|child| matches!(child.try_wait(), Ok(None)))
                                .unwrap_or(false);
                            if !alive {
                                break;
                            }
                            thread::sleep(Duration::from_millis(200));
                        }
                    }
                }
                supervisor.terminate_child();
                let _ = app.emit("sidecar-stopped", ());
                if started.elapsed() > Duration::from_secs(60) {
                    backoff = 1;
                }
                // Interruptible backoff; window closure never waits for a full sleep.
                for _ in 0..backoff * 10 {
                    if supervisor.0.stopping.load(Ordering::SeqCst) {
                        break;
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                backoff = (backoff * 2).min(30);
            }
            supervisor.terminate_child();
        });
    }
}
