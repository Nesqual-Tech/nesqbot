//! Credential storage for the desktop shell.
//!
//! # What this protects, and what it does not
//!
//! Values go into the operating system's credential store — Windows Credential
//! Manager, macOS Keychain, Linux Secret Service — under the service name
//! [`SERVICE`]. That is the strongest option available to a Tauri app without
//! asking the user for a second password:
//!
//!  - **Protected:** the credential is not a readable file in the profile
//!    directory. On Windows it is DPAPI-sealed to the signed-in user account, so
//!    another local user, a stolen disk image, or a backup copy of `AppData`
//!    does not yield the token. `tauri-plugin-store` — the obvious alternative —
//!    writes plaintext JSON that any of those three reads directly.
//!  - **Not protected:** anything already running as this user. The commands
//!    below are reachable from the webview, so script execution inside the
//!    frontend (XSS in our own UI, a malicious dependency) can ask for the
//!    refresh token exactly as the auth module does. No client-side store can
//!    fix that; only a shorter-lived credential or a server-held one can.
//!
//! The refresh token is a long-lived credential and is treated as one: it lives
//! here, never in `localStorage`, and is never logged or returned in an error
//! message.
//!
//! # Why values are chunked
//!
//! Windows caps a credential blob at 2560 bytes and `keyring` stores the value
//! as UTF-16, so roughly 1280 characters. Entra access and refresh tokens
//! regularly exceed that. A value is therefore split across `key`, `key#1`,
//! `key#2`, … and reassembled on read. Writes clear the old chunks first, so
//! "read until `NoEntry`" cannot pick up a tail left by a longer previous value.

use keyring::{Entry, Error as KeyringError};

/// Matches the bundle identifier so the entries are attributable in the OS UI.
/// Credential-store namespace, taken from the bundle identifier at compile time.
///
/// This was a hardcoded `"com.nesqualtech.nesqbot"`, which made every build on a
/// developer's machine share one keyring entry with the installed production app
/// - including test builds given a *different* Tauri identifier specifically to
/// keep them isolated. Since `restore()` reads the refresh token on mount and
/// posts it to the live tenant, merely launching a scratch build silently renewed
/// the real user's session. An agent verifying UI work declined to run any
/// packaged build here for exactly that reason, which is how it was found.
///
/// `CARGO_PKG_NAME` tracks the crate, so an alternate build that changes its
/// identifier and package name gets its own namespace and cannot reach the real
/// credentials. Note this means renaming the crate orphans existing entries -
/// users would sign in once more, which is the correct trade against a scratch
/// build touching production tokens.
const SERVICE: &str = concat!("com.nesqualtech.", env!("CARGO_PKG_NAME"));

/// Characters per chunk. 1000 UTF-16 units = 2000 bytes, inside Windows' 2560.
const CHUNK_CHARS: usize = 1000;

/// Upper bound on chunks (~64 000 characters), so a corrupt store cannot spin.
const MAX_CHUNKS: usize = 64;

/// The frontend picks the key, so constrain it. The service name already scopes
/// every entry to this app, but a narrow charset keeps a compromised frontend
/// from constructing surprising entry names.
fn valid_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 64
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-' || b == b'_')
}

/// Error text for the frontend. Carries the failure, never the value.
fn describe(err: KeyringError) -> String {
    format!("credential store: {err}")
}

fn entry(key: &str, index: usize) -> Result<Entry, String> {
    let name = if index == 0 {
        key.to_string()
    } else {
        format!("{key}#{index}")
    };
    Entry::new(SERVICE, &name).map_err(describe)
}

fn clear_chunks(key: &str) -> Result<(), String> {
    for index in 0..MAX_CHUNKS {
        match entry(key, index)?.delete_credential() {
            Ok(()) => {}
            Err(KeyringError::NoEntry) => return Ok(()),
            Err(err) => return Err(describe(err)),
        }
    }
    Ok(())
}

/// Reads a value previously written by [`secret_set`], or `None` when absent.
#[tauri::command]
pub fn secret_get(key: String) -> Result<Option<String>, String> {
    if !valid_key(&key) {
        return Err("invalid credential key".into());
    }
    let mut value = String::new();
    let mut found = false;
    for index in 0..MAX_CHUNKS {
        match entry(&key, index)?.get_password() {
            Ok(part) => {
                value.push_str(&part);
                found = true;
            }
            Err(KeyringError::NoEntry) => break,
            Err(err) => return Err(describe(err)),
        }
    }
    Ok(if found { Some(value) } else { None })
}

/// Stores a value, replacing whatever was there. An empty value deletes.
#[tauri::command]
pub fn secret_set(key: String, value: String) -> Result<(), String> {
    if !valid_key(&key) {
        return Err("invalid credential key".into());
    }
    clear_chunks(&key)?;
    if value.is_empty() {
        return Ok(());
    }

    let chars: Vec<char> = value.chars().collect();
    if chars.len() > CHUNK_CHARS * MAX_CHUNKS {
        return Err("credential store: value too large".into());
    }
    for (index, chunk) in chars.chunks(CHUNK_CHARS).enumerate() {
        let part: String = chunk.iter().collect();
        entry(&key, index)?.set_password(&part).map_err(describe)?;
    }
    Ok(())
}

/// Removes a value. Absent is success — sign-out must never fail on this.
#[tauri::command]
pub fn secret_delete(key: String) -> Result<(), String> {
    if !valid_key(&key) {
        return Err("invalid credential key".into());
    }
    clear_chunks(&key)
}
