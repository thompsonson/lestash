use std::marker::PhantomData;

use serde::de::DeserializeOwned;
use tauri::{plugin::PluginApi, AppHandle, Runtime};

use crate::error::{Error, Result};
use crate::models::{AuthorizeRequest, AuthorizeResponse};

pub fn init<R: Runtime, C: DeserializeOwned>(
    _app: &AppHandle<R>,
    _api: PluginApi<R, C>,
) -> crate::error::Result<GoogleAuth<R>> {
    Ok(GoogleAuth(PhantomData))
}

pub struct GoogleAuth<R: Runtime>(PhantomData<R>);

// Safety: GoogleAuth holds only PhantomData<R> and no actual R instance,
// so it is safe to send/share across threads regardless of R's own bounds.
// Required by tauri::Manager::state/manage since Tauri 2.10.
unsafe impl<R: Runtime> Send for GoogleAuth<R> {}
unsafe impl<R: Runtime> Sync for GoogleAuth<R> {}

impl<R: Runtime> GoogleAuth<R> {
    pub async fn authorize(&self, _req: AuthorizeRequest) -> Result<AuthorizeResponse> {
        Err(Error::UnsupportedPlatform)
    }
}
