use reqwest::multipart;
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
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TranscribeResult {
    pub text: String,
    pub title: String,
    pub item_id: i64,
    pub duration_seconds: f64,
}

fn pending_share_path(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .cache_dir()
        .unwrap_or_else(|_| std::env::temp_dir())
        .join("pending_share.json")
}

#[tauri::command]
pub async fn check_pending_share(
    app: tauri::AppHandle,
) -> Result<Option<PendingShare>, String> {
    let path = pending_share_path(&app);
    if !path.exists() {
        return Ok(None);
    }

    let content = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let _ = std::fs::remove_file(&path);

    let share: PendingShare = serde_json::from_str(&content).map_err(|e| e.to_string())?;
    Ok(Some(share))
}

#[tauri::command]
pub async fn upload_shared_audio(
    file_path: String,
    file_name: String,
    host: String,
    port: u16,
) -> Result<TranscribeResult, String> {
    let path = std::path::Path::new(&file_path);
    if !path.exists() {
        return Err(format!("Shared file not found: {file_path}"));
    }

    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;

    // Determine MIME type from extension
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
        _ => "application/octet-stream",
    };

    let title = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("shared_audio")
        .to_string();

    let file_part = multipart::Part::bytes(bytes)
        .file_name(file_name)
        .mime_str(mime_type)
        .map_err(|e| e.to_string())?;

    let form = multipart::Form::new()
        .part("file", file_part)
        .text("title", title);

    let url = format!("https://{host}:{port}/api/voice/transcribe");

    let client = reqwest::Client::builder()
        .danger_accept_invalid_certs(true)
        .build()
        .map_err(|e| e.to_string())?;

    let res = client
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("Upload failed: {e}"))?;

    if !res.status().is_success() {
        let status = res.status();
        let body = res.text().await.unwrap_or_default();
        return Err(format!("Server error {status}: {body}"));
    }

    #[derive(Deserialize)]
    struct ServerResponse {
        text: String,
        title: String,
        item_id: i64,
        duration_seconds: f64,
    }

    let data: ServerResponse = res.json().await.map_err(|e| e.to_string())?;

    // Clean up the temp file
    let _ = std::fs::remove_file(path);

    Ok(TranscribeResult {
        text: data.text,
        title: data.title,
        item_id: data.item_id,
        duration_seconds: data.duration_seconds,
    })
}
