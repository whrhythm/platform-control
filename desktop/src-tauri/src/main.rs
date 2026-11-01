#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod supervisor;
use supervisor::{Info, Supervisor};
use tauri::Manager;

#[tauri::command]
fn sidecar_info(
    window: tauri::Window,
    state: tauri::State<'_, Supervisor>,
) -> Result<Info, String> {
    if window.label() != "main" {
        return Err("Window not authorized".into());
    }
    state.info()
}

fn main() {
    tauri::Builder::default()
        .manage(Supervisor::default())
        .invoke_handler(tauri::generate_handler![sidecar_info])
        .setup(|app| {
            app.state::<Supervisor>().start(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(
                event,
                tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
            ) {
                window.state::<Supervisor>().stop();
                window.app_handle().exit(0);
            }
        })
        .build(tauri::generate_context!())
        .expect("Unable to start desktop UI")
        .run(|app, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                app.state::<Supervisor>().stop();
            }
        });
}
