#!/usr/bin/env node
/*
 * The desktop app's version has to be one number in four places, and the
 * newest installer must not be ahead of the source that claims to build it.
 *
 * What went wrong without this check: source sat at 0.6.2 while
 * `dist/installer` held builds up to `Nesq Bot_0.8.7_x64-setup.exe`. Five
 * releases — 0.7.0, 0.8.0, 0.8.1, 0.8.3 … 0.8.7 — were built from a working
 * tree whose version bumps were never committed, so git's last word on the
 * desktop version is 0.6.2 (commit 29a6ca6, 2026-08-26) and the installed app
 * reported 0.8.7. Nobody could answer "what is the latest version" from the
 * repository, which is the one place that question should be answerable.
 *
 * Tauri reads `tauri.conf.json`; the crate carries its own `Cargo.toml`
 * version; npm reads `package.json`; and the NSIS bundle is *named* from the
 * Tauri one, which is why a mismatch shows up as an installer whose filename
 * disagrees with the app's own about-box.
 *
 * Run with `npm run check:versions` (also `npm run lint`), or
 * `node scripts/check-versions.mjs --bump 0.9.1` to set all four at once.
 */

import { readFileSync, writeFileSync, readdirSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const desktop = resolve(here, "..");
const repo = resolve(desktop, "..", "..");

const FILES = {
  "apps/desktop/src-tauri/tauri.conf.json": join(desktop, "src-tauri", "tauri.conf.json"),
  "apps/desktop/package.json": join(desktop, "package.json"),
  "package.json": join(repo, "package.json"),
  "apps/desktop/src-tauri/Cargo.toml": join(desktop, "src-tauri", "Cargo.toml"),
};

const readVersion = (label, path) => {
  const text = readFileSync(path, "utf8");
  if (path.endsWith(".toml")) {
    const match = text.match(/^version = "([^"]+)"/m);
    if (!match) throw new Error(`${label}: no [package] version`);
    return match[1];
  }
  const version = JSON.parse(text).version;
  if (!version) throw new Error(`${label}: no "version" field`);
  return version;
};

const writeVersion = (label, path, next) => {
  const text = readFileSync(path, "utf8");
  const updated = path.endsWith(".toml")
    ? text.replace(/^version = "[^"]+"/m, `version = "${next}"`)
    : text.replace(/("version"\s*:\s*")[^"]+(")/, `$1${next}$2`);
  if (updated === text) throw new Error(`${label}: version not replaced`);
  writeFileSync(path, updated);
  console.log(`  ${label} -> ${next}`);
};

/** Highest semver among the installers actually sitting in dist/installer. */
const newestInstaller = () => {
  const dir = join(repo, "dist", "installer");
  if (!existsSync(dir)) return null;
  let best = null;
  for (const name of readdirSync(dir)) {
    const match = name.match(/(\d+)\.(\d+)\.(\d+)/);
    if (!match) continue;
    const parts = match.slice(1, 4).map(Number);
    if (!best || compare(parts, best.parts) > 0) best = { name, parts };
  }
  return best;
};

const compare = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
const parse = (v) => v.split(".").map(Number);

const bumpTo = process.argv.includes("--bump")
  ? process.argv[process.argv.indexOf("--bump") + 1]
  : null;

if (bumpTo) {
  if (!/^\d+\.\d+\.\d+$/.test(bumpTo)) {
    console.error(`--bump needs a plain x.y.z version, got "${bumpTo}"`);
    process.exit(2);
  }
  console.log(`Setting the desktop version to ${bumpTo}:`);
  for (const [label, path] of Object.entries(FILES)) writeVersion(label, path, bumpTo);
  process.exit(0);
}

const found = Object.entries(FILES).map(([label, path]) => [label, readVersion(label, path)]);
const versions = new Set(found.map(([, v]) => v));
const problems = [];

if (versions.size !== 1) {
  problems.push(
    "the four version fields disagree:\n" +
      found.map(([label, v]) => `    ${v.padStart(8)}  ${label}`).join("\n") +
      "\n  Set them together: node scripts/check-versions.mjs --bump <x.y.z>",
  );
}

const source = found[0][1];
const installer = newestInstaller();
if (versions.size === 1 && installer && compare(installer.parts, parse(source)) > 0) {
  problems.push(
    `dist/installer holds "${installer.name}", which is newer than the source version ` +
      `${source}.\n  That is the drift this check exists for: a release was built from an ` +
      `uncommitted\n  bump, so the repository can no longer say what the latest version is. ` +
      `Bump to\n  ${installer.parts.join(".")} or higher and commit it.`,
  );
}

if (problems.length) {
  console.error("check-versions: " + problems.join("\n\ncheck-versions: "));
  process.exit(1);
}

console.log(
  `check-versions: desktop ${source} in all four files` +
    (installer ? `; newest installer ${installer.name}` : "; no installers on disk"),
);
