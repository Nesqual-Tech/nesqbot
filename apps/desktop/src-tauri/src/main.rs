// Release builds are GUI apps: without this the Windows loader marks the
// binary as a console subsystem app and every launch pops a cmd window behind
// the Tauri window. Kept conditional on `not(debug_assertions)` so a debug
// build still gets a console and `println!` still goes somewhere visible.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    nesqbot_lib::run()
}
