use tauri::{AppHandle, Runtime};

use crate::error::Result;
use crate::models::{AuthorizeRequest, AuthorizeResponse};
use crate::GoogleAuthExt;

#[tauri::command]
pub(crate) async fn start_google_auth<R: Runtime>(
    app: AppHandle<R>,
    payload: AuthorizeRequest,
) -> Result<AuthorizeResponse> {
    app.google_auth().authorize(payload).await
}
