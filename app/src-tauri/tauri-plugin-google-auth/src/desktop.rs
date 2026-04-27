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

impl<R: Runtime> GoogleAuth<R> {
    pub async fn authorize(&self, _req: AuthorizeRequest) -> Result<AuthorizeResponse> {
        Err(Error::UnsupportedPlatform)
    }
}
