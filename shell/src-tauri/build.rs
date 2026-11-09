fn main() {
    // the one IPC command the page may call (its permission is generated
    // here as `allow-api-token`, named in capabilities/main.json)
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["api_token"])),
    )
    .expect("tauri-build");
}
