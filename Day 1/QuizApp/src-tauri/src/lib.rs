use std::path::PathBuf;
use std::process::Command;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend_path: PathBuf = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("Could not find project root")
        .join("backend.py");

    println!("Starting backend: {}", backend_path.display());

    let result = Command::new("python")
        .current_dir(
            backend_path
                .parent()
                .expect("Could not find project root"),
        )
        .arg(&backend_path)
        .spawn();

    match result {
        Ok(_) => println!("FastAPI backend started"),
        Err(e) => eprintln!("Failed to start FastAPI backend: {}", e),
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}