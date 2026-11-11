fn main() {
    // the IPC commands the page may call (their permissions are generated
    // here as `allow-api-token` and `allow-pick-folder`, named in
    // capabilities/main.json; pick_folder is 6b628's folder picker)
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["api_token", "pick_folder"])),
    )
    .expect("tauri-build");
}
