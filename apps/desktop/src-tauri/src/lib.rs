//! Nesq Bot desktop shell.
//!
//! Adds a native window menu, a tray icon (show/hide + quit), `nesqbot://`
//! deep-link handling and the native half of Microsoft Entra sign-in on top of
//! the Tauri 2 webview. Shell-to-UI communication is plain `emit` events
//! consumed through `window.__TAURI__.event.listen`.
//!
//! Sign-in needs the shell for exactly two things the webview cannot do:
//!
//!  - **redeeming the authorization code.** See `entra.rs`. The POST is made by
//!    a `reqwest` client owned by this process, which sends no `Origin` header,
//!    so Entra treats it as the native public client it is instead of refusing
//!    it with `AADSTS9002326`. Deliberately not `tauri-plugin-http`: that plugin
//!    forwards the webview's origin and failed the same way.
//!  - **storing the refresh token.** See `secrets.rs`; it goes into the OS
//!    credential store rather than `localStorage`.
//!
//! Opening the browser is [`open_sign_in_url`] rather than a blanket
//! `shell:allow-open` for the frontend, so the one URL the UI can launch is a
//! Microsoft sign-in URL.

mod entra;
mod secrets;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Runtime, WebviewWindow};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_shell::ShellExt;

const MAIN_WINDOW: &str = "main";
const DOCS_URL: &str = "https://github.com/nesqualtech/nesqbot";

/// The only authority the UI may send the user to.
const ENTRA_AUTHORITY: &str = "https://login.microsoftonline.com/";

/// Event carrying a window-menu command to the UI.
const MENU_EVENT: &str = "menu";
/// Event carrying one or more `nesqbot://` URLs to the UI.
const DEEP_LINK_EVENT: &str = "deep-link";

fn main_window<R: Runtime>(app: &AppHandle<R>) -> Option<WebviewWindow<R>> {
    app.get_webview_window(MAIN_WINDOW)
}

fn show_main<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = main_window(app) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn hide_main<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = main_window(app) {
        let _ = window.hide();
    }
}

fn toggle_main<R: Runtime>(app: &AppHandle<R>) {
    match main_window(app) {
        Some(window) => match window.is_visible() {
            Ok(true) => {
                let _ = window.hide();
            }
            _ => {
                let _ = window.show();
                let _ = window.set_focus();
            }
        },
        None => {}
    }
}

fn emit_menu<R: Runtime>(app: &AppHandle<R>, command: &str) {
    let _ = app.emit(MENU_EVENT, command.to_string());
}

fn build_app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let new_thread = MenuItem::with_id(app, "new-thread", "New thread", true, Some("CmdOrCtrl+N"))?;
    let quit = PredefinedMenuItem::quit(app, Some("Quit Nesq Bot"))?;

    let file = Submenu::with_items(
        app,
        "File",
        true,
        &[&new_thread, &PredefinedMenuItem::separator(app)?, &quit],
    )?;

    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;

    let approvals = MenuItem::with_id(app, "open-approvals", "Approvals", true, Some("CmdOrCtrl+1"))?;
    let integrations =
        MenuItem::with_id(app, "open-integrations", "Integrations", true, Some("CmdOrCtrl+2"))?;
    let routines = MenuItem::with_id(app, "open-routines", "Routines", true, Some("CmdOrCtrl+3"))?;
    let usage = MenuItem::with_id(app, "open-usage", "Usage", true, Some("CmdOrCtrl+4"))?;
    let theme = MenuItem::with_id(
        app,
        "toggle-theme",
        "Toggle light / dark",
        true,
        Some("CmdOrCtrl+Shift+L"),
    )?;
    let reload = MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?;

    let view = Submenu::with_items(
        app,
        "View",
        true,
        &[
            &approvals,
            &integrations,
            &routines,
            &usage,
            &PredefinedMenuItem::separator(app)?,
            &theme,
            &reload,
        ],
    )?;

    let docs = MenuItem::with_id(app, "open-docs", "Documentation", true, None::<&str>)?;
    let help = Submenu::with_items(app, "Help", true, &[&docs])?;

    Menu::with_items(app, &[&file, &edit, &view, &help])
}

fn build_tray<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "tray-show", "Show Nesq Bot", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "tray-hide", "Hide window", true, None::<&str>)?;
    let approvals = MenuItem::with_id(app, "tray-approvals", "Open approvals", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray-quit", "Quit", true, None::<&str>)?;

    let menu = Menu::with_items(
        app,
        &[
            &show,
            &hide,
            &approvals,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    let mut builder = TrayIconBuilder::with_id("nesqbot-tray")
        .tooltip("Nesq Bot")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray-show" => show_main(app),
            "tray-hide" => hide_main(app),
            "tray-approvals" => {
                show_main(app);
                emit_menu(app, "open-approvals");
            }
            "tray-quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main(tray.app_handle());
            }
        });

    // Comes from `bundle.icon` (icons/*.png + icon.ico). Still optional rather
    // than unwrapped, so a stripped build degrades to a menu-only tray.
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }

    builder.build(app)?;
    Ok(())
}

fn handle_menu_command<R: Runtime>(app: &AppHandle<R>, id: &str) {
    match id {
        "open-docs" => {
            let _ = app.shell().open(DOCS_URL, None);
        }
        "new-thread" | "toggle-theme" | "reload" | "open-approvals" | "open-integrations"
        | "open-routines" | "open-usage" => {
            show_main(app);
            emit_menu(app, id);
        }
        _ => {}
    }
}

fn register_deep_links<R: Runtime>(app: &AppHandle<R>) {
    // Runtime registration is only needed on Linux, and on Windows in dev.
    #[cfg(any(target_os = "linux", all(debug_assertions, windows)))]
    {
        let _ = app.deep_link().register_all();
    }

    let handle = app.clone();
    app.deep_link().on_open_url(move |event| {
        let urls: Vec<String> = event.urls().iter().map(|url| url.to_string()).collect();
        show_main(&handle);
        let _ = handle.emit(DEEP_LINK_EVENT, urls);
    });
}

/// Opens the Entra authorize URL in the user's real browser.
///
/// The URL is built by the frontend and carries the PKCE *challenge* (a hash,
/// not the verifier) and the `state` nonce, so there is nothing secret in it —
/// but it is still checked against [`ENTRA_AUTHORITY`] before being handed to
/// the OS, because "open whatever the webview asks for" is a phishing primitive.
/// The rejection message deliberately does not echo the URL back.
#[tauri::command]
fn open_sign_in_url(app: AppHandle, url: String) -> Result<(), String> {
    if !url.starts_with(ENTRA_AUTHORITY) {
        return Err("refused: not a Microsoft sign-in URL".into());
    }
    app.shell()
        .open(url, None)
        .map_err(|err| format!("could not open the browser: {err}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[allow(unused_mut)]
    let mut builder = tauri::Builder::default();

    // Must come before the deep-link plugin. Windows and Linux do not hand a
    // URL to a running process — they launch a second one with the URL as its
    // only argument — so without this the `nesqbot://auth` redirect would start
    // a fresh Nesq Bot that knows nothing about the sign-in waiting in the
    // first. The `deep-link` feature forwards that argument to the running
    // instance, which is what makes `on_open_url` below fire.
    #[cfg(desktop)]
    {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main(app);
        }));
    }

    builder
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_deep_link::init())
        .invoke_handler(tauri::generate_handler![
            open_sign_in_url,
            entra::redeem_entra_code,
            entra::refresh_entra_token,
            secrets::secret_get,
            secrets::secret_set,
            secrets::secret_delete,
        ])
        .setup(|app| {
            let handle = app.handle().clone();

            let menu = build_app_menu(&handle)?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, event| handle_menu_command(app, event.id().as_ref()));

            build_tray(&handle)?;
            register_deep_links(&handle);

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Nesq Bot");
}
