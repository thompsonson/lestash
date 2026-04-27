const COMMANDS: &[&str] = &["start_google_auth"];

fn main() {
    tauri_plugin::Builder::new(COMMANDS)
        .android_path("android")
        .build();
}
