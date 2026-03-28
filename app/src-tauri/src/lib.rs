mod share;
mod stt;

use std::sync::Arc;

struct AppState {
    stt: Arc<stt::SttEngine>,
}

#[tauri::command]
async fn start_recording(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<String, String> {
    state
        .stt
        .start(app)
        .await
        .map_err(|e| e.to_string())?;
    Ok("Recording started".into())
}

#[tauri::command]
async fn stop_recording(state: tauri::State<'_, AppState>) -> Result<String, String> {
    state.stt.stop().await;
    Ok("Recording stopped".into())
}

#[tauri::command]
fn get_stt_status(app: tauri::AppHandle, state: tauri::State<'_, AppState>) -> serde_json::Value {
    let path = stt::model_path_for_app(&app);
    serde_json::json!({
        "listening": state.stt.is_listening(),
        "model_exists": path.exists(),
        "model_path": path.to_string_lossy(),
    })
}

#[tauri::command]
async fn download_model(app: tauri::AppHandle) -> Result<String, String> {
    stt::download_model(app).await.map_err(|e| e.to_string())
}

pub fn run() {
    tracing_subscriber::fmt::init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            stt: Arc::new(stt::SttEngine::new()),
        })
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            get_stt_status,
            download_model,
            share::check_pending_share,
            share::read_shared_file,
        ])
        .run(tauri::generate_context!())
        .expect("error running lestash");
}

#[cfg(mobile)]
#[tauri::mobile_entry_point]
pub fn mobile_entry() {
    run();
}
