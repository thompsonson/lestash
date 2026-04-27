use std::sync::Arc;

use tauri::{
    plugin::{Builder, TauriPlugin},
    Manager, Runtime,
};

mod commands;
mod error;
mod models;

#[cfg(mobile)]
mod mobile;
#[cfg(not(mobile))]
mod desktop;

pub use error::{Error, Result};
pub use models::{AuthorizeRequest, AuthorizeResponse};

#[cfg(mobile)]
use mobile::GoogleAuth;
#[cfg(not(mobile))]
use desktop::GoogleAuth;

pub trait GoogleAuthExt<R: Runtime> {
    fn google_auth(&self) -> GoogleAuthHandle<R>;
}

pub struct GoogleAuthHandle<R: Runtime> {
    inner: Arc<GoogleAuth<R>>,
}

impl<R: Runtime> GoogleAuthHandle<R> {
    pub async fn authorize(&self, req: AuthorizeRequest) -> Result<AuthorizeResponse> {
        self.inner.authorize(req).await
    }
}

impl<R: Runtime, T: Manager<R>> GoogleAuthExt<R> for T {
    fn google_auth(&self) -> GoogleAuthHandle<R> {
        let state = self.state::<PluginState<R>>();
        GoogleAuthHandle {
            inner: state.inner.clone(),
        }
    }
}

struct PluginState<R: Runtime> {
    inner: Arc<GoogleAuth<R>>,
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("google-auth")
        .invoke_handler(tauri::generate_handler![commands::start_google_auth])
        .setup(|app, api| {
            #[cfg(mobile)]
            let inner = mobile::init(app, api)?;
            #[cfg(not(mobile))]
            let inner = desktop::init(app, api)?;

            app.manage(PluginState {
                inner: Arc::new(inner),
            });
            Ok(())
        })
        .build()
}
