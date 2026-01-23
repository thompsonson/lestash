# Native cr-sqlite Libraries

This directory contains pre-built cr-sqlite native libraries for mobile platforms.

## Directory Structure

```
native/
├── android/
│   ├── armeabi-v7a/    # ARM 32-bit
│   │   └── libcrsqlite.so
│   ├── arm64-v8a/      # ARM 64-bit (most modern devices)
│   │   └── libcrsqlite.so
│   ├── x86/            # Intel 32-bit (emulators)
│   │   └── libcrsqlite.so
│   └── x86_64/         # Intel 64-bit (emulators)
│       └── libcrsqlite.so
└── ios/
    └── crsqlite.xcframework/
```

## Building the Libraries

The cr-sqlite libraries are built via GitHub Actions CI workflow.

### Android

To build cr-sqlite for Android:

1. Clone the cr-sqlite repository: https://github.com/vlcn-io/cr-sqlite
2. Follow their build instructions for Android NDK cross-compilation
3. Or use our CI workflow which automates this process

### iOS

To build cr-sqlite for iOS:

1. Clone the cr-sqlite repository
2. Build as an XCFramework for device and simulator architectures
3. Place the resulting `crsqlite.xcframework` in the `ios/` directory

## CI Integration

The GitHub Actions workflow at `.github/workflows/build-crsqlite.yml` automatically:

1. Builds cr-sqlite for all Android architectures
2. Builds cr-sqlite XCFramework for iOS
3. Uploads the libraries as artifacts
4. Creates releases with the binary assets

## Version

The cr-sqlite version should match the version used on the desktop Python side.
Current target version: See `packages/lestash/src/lestash/core/crsqlite.py` for the version constant.

## Loading the Extension

The Flutter app loads these libraries at runtime using the `sqlite3` package's FFI capabilities.
See `lib/core/database/database.dart` for the loading logic.
