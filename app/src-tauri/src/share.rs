use base64::Engine;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use tauri::Manager;

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PendingShare {
    pub mime_type: String,
    #[serde(default)]
    pub file_path: Option<String>,
    #[serde(default)]
    pub file_name: Option<String>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub subject: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedFileData {
    pub base64: String,
    pub file_name: String,
    pub mime_type: String,
}

/// Return candidate paths where Kotlin's `cacheDir` might have written
/// `pending_share.json`. Tauri's `cache_dir()` may differ from Android's
/// `Context.getCacheDir()`, so we check multiple locations.
fn pending_share_candidates(app: &tauri::AppHandle) -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    // Tauri's cache_dir (may include app identifier subdirectory)
    if let Ok(p) = app.path().cache_dir() {
        candidates.push(p.join("pending_share.json"));
    }

    // Tauri's app_cache_dir
    if let Ok(p) = app.path().app_cache_dir() {
        candidates.push(p.join("pending_share.json"));
    }

    // Android's typical Context.getCacheDir() path
    candidates.push(PathBuf::from("/data/data/dev.lestash.app/cache/pending_share.json"));
    candidates.push(PathBuf::from("/data/user/0/dev.lestash.app/cache/pending_share.json"));

    // Fallback to temp dir
    candidates.push(std::env::temp_dir().join("pending_share.json"));

    candidates
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ShareCheckDebug {
    pub share: Option<PendingShare>,
    pub checked_paths: Vec<String>,
    pub found_at: Option<String>,
}

#[tauri::command]
pub async fn check_pending_share(
    app: tauri::AppHandle,
) -> Result<ShareCheckDebug, String> {
    let candidates = pending_share_candidates(&app);
    let checked: Vec<String> = candidates.iter().map(|p| p.display().to_string()).collect();

    for path in &candidates {
        if path.exists() {
            let found = path.display().to_string();
            let content = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
            let _ = std::fs::remove_file(path);
            let share: PendingShare =
                serde_json::from_str(&content).map_err(|e| e.to_string())?;
            return Ok(ShareCheckDebug {
                share: Some(share),
                checked_paths: checked,
                found_at: Some(found),
            });
        }
    }

    Ok(ShareCheckDebug {
        share: None,
        checked_paths: checked,
        found_at: None,
    })
}

/// Read a shared file and return its contents as base64 so the frontend
/// can upload it via fetch (which uses Android's native network stack and
/// works through Tailscale VPN, unlike reqwest).
#[tauri::command]
pub async fn read_shared_file(
    file_path: String,
    file_name: String,
) -> Result<SharedFileData, String> {
    let path = std::path::Path::new(&file_path);
    if !path.exists() {
        return Err(format!("Shared file not found: {file_path}"));
    }

    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;

    let mime_type = match path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase()
        .as_str()
    {
        "m4a" => "audio/mp4",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "ogg" => "audio/ogg",
        "flac" => "audio/flac",
        "webm" => "audio/webm",
        "html" | "htm" => "text/html",
        "xhtml" => "application/xhtml+xml",
        _ => "application/octet-stream",
    };

    let base64 = base64::engine::general_purpose::STANDARD.encode(&bytes);

    // Clean up the temp file
    let _ = std::fs::remove_file(path);

    Ok(SharedFileData {
        base64,
        file_name,
        mime_type: mime_type.to_string(),
    })
}
