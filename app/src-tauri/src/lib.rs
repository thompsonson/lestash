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

// Opens a web or mail link in the system browser or mail app, through the opener
// plugin (on Android, an ACTION_VIEW intent). The shell plugin's open, used before,
// is deprecated, and links did nothing on Android with it (#328). Only http, https
// and mailto pass.
//
// Async, and the plugin call on a blocking thread: on Android the call is run on the
// app's main thread and this side waits for its answer, so it must not wait on the
// main thread itself (#328).
#[tauri::command]
async fn open_external(app: tauri::AppHandle, url: String) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    let lower = url.to_ascii_lowercase();
    if !["https://", "http://", "mailto:"].iter().any(|p| lower.starts_with(p)) {
        return Err(format!("not a web or mail link: {url}"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn get_app_version(app: tauri::AppHandle) -> String {
    app.config().version.clone().unwrap_or_else(|| "unknown".into())
}

pub fn run() {
    tracing_subscriber::fmt::init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_google_auth::init())
        .manage(AppState {
            stt: Arc::new(stt::SttEngine::new()),
        })
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            get_stt_status,
            download_model,
            get_app_version,
            open_external,
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
