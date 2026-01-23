# Android cr-sqlite Libraries

Place the compiled `libcrsqlite.so` files in the appropriate architecture directories:

- `armeabi-v7a/` - ARM 32-bit (older devices)
- `arm64-v8a/` - ARM 64-bit (most modern Android phones)
- `x86/` - Intel 32-bit (some emulators)
- `x86_64/` - Intel 64-bit (most emulators)

## Building

The libraries are built from the cr-sqlite source using the Android NDK.

### Requirements

- Android NDK r25 or later
- Rust toolchain with Android targets:
  ```bash
  rustup target add aarch64-linux-android
  rustup target add armv7-linux-androideabi
  rustup target add x86_64-linux-android
  rustup target add i686-linux-android
  ```

### Build Commands

See the CI workflow for exact build commands.

## Note

These `.so` files are git-ignored. They will be provided via:
1. CI build artifacts
2. Release downloads
3. Local builds

The Flutter app copies these to the appropriate location at build time via
the Gradle build configuration.
