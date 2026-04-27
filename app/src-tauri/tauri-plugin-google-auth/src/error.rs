use serde::{Serialize, Serializer};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("google auth is only available on Android in this build")]
    UnsupportedPlatform,
    #[error("user cancelled the authorization flow")]
    Cancelled,
    #[error("google identity services unavailable: {0}")]
    Unavailable(String),
    #[error("{0}")]
    Other(String),
    #[cfg(target_os = "android")]
    #[error(transparent)]
    Jni(#[from] jni::errors::Error),
}

impl Serialize for Error {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(self.to_string().as_ref())
    }
}

pub type Result<T> = std::result::Result<T, Error>;
