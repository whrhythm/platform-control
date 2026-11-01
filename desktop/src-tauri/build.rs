fn main() {
    println!(
        "cargo:rustc-env=PLATFORM_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
    tauri_build::build();
}
