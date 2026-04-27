use serde::de::DeserializeOwned;
use tauri::{
    plugin::{PluginApi, PluginHandle},
    AppHandle, Runtime,
};

use crate::error::Result;
use crate::models::{AuthorizeRequest, AuthorizeResponse};

#[cfg(target_os = "android")]
const PLUGIN_IDENTIFIER: &str = "dev.lestash.plugin.googleauth";

pub fn init<R: Runtime, C: DeserializeOwned>(
    _app: &AppHandle<R>,
    api: PluginApi<R, C>,
) -> crate::error::Result<GoogleAuth<R>> {
    #[cfg(target_os = "android")]
    let handle = api.register_android_plugin(PLUGIN_IDENTIFIER, "GoogleAuthPlugin")?;
    #[cfg(target_os = "ios")]
    let handle = api.register_ios_plugin(init_plugin_google_auth)?;

    Ok(GoogleAuth(handle))
}

pub struct GoogleAuth<R: Runtime>(PluginHandle<R>);

impl<R: Runtime> GoogleAuth<R> {
    pub async fn authorize(&self, req: AuthorizeRequest) -> Result<AuthorizeResponse> {
        self.0
            .run_mobile_plugin::<AuthorizeResponse>("authorize", req)
            .map_err(Into::into)
    }
}

impl From<tauri::plugin::mobile::PluginInvokeError> for crate::error::Error {
    fn from(value: tauri::plugin::mobile::PluginInvokeError) -> Self {
        crate::error::Error::Other(value.to_string())
    }
}
