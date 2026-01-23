# iOS cr-sqlite XCFramework

Place the compiled `crsqlite.xcframework` directory here.

## Structure

The XCFramework should contain slices for:
- `ios-arm64` - Physical iOS devices
- `ios-arm64-simulator` - Apple Silicon Mac simulators
- `ios-arm64_x86_64-simulator` - Universal simulator (Intel + Apple Silicon)

## Building

### Requirements

- Xcode 14 or later
- Rust toolchain with iOS targets:
  ```bash
  rustup target add aarch64-apple-ios
  rustup target add aarch64-apple-ios-sim
  rustup target add x86_64-apple-ios
  ```

### Build Commands

See the CI workflow for exact build commands.

## Note

The XCFramework is git-ignored. It will be provided via:
1. CI build artifacts
2. Release downloads
3. Local builds

The Flutter iOS build integrates this via the Podfile configuration.
