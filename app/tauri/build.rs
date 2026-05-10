fn main() {
    // tauri.conf.json points frontendDist at ../empty-dist as a placeholder —
    // the app loads an external Gradio URL at runtime and never serves these
    // files, but tauri::generate_context!() validates the path at compile
    // time. The directory is gitignored, so create it here so a fresh
    // checkout (CI or otherwise) builds without a separate setup step.
    let dist = std::path::Path::new("../empty-dist");
    std::fs::create_dir_all(dist).expect("create ../empty-dist");
    let index = dist.join("index.html");
    if !index.exists() {
        std::fs::write(&index, "<!doctype html><title>bfagent</title>\n")
            .expect("write ../empty-dist/index.html");
    }

    tauri_build::build()
}
