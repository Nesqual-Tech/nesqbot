// Metro configuration for the Nesq Bot mobile app.
//
// `apps/mobile` is not an npm workspace (see the "//workspaces" note in the root
// package.json — the React 18 / React 19 split makes a shared hoisted tree unbundleable).
// It installs on its own and declares the shared packages as `file:../../packages/*`,
// which npm resolves to SYMLINKS into `apps/mobile/node_modules/@nesqbot/`.
//
// That means `@nesqbot/ui` and `@nesqbot/protocol` are ordinary, declared dependencies
// and resolve through ordinary node_modules lookup — there is deliberately no alias for
// them here any more. The only thing Metro still needs is to be told to WATCH the real
// directories the symlinks point at, so an edit in `packages/*` fast-refreshes the app.
const { getDefaultConfig } = require("expo/metro-config")
const path = require("path")

const projectRoot = __dirname
const workspaceRoot = path.resolve(projectRoot, "..", "..")

/** @type {import('expo/metro-config').MetroConfig} */
const config = getDefaultConfig(projectRoot)

// 1. Watch the shared packages through their REAL paths. Metro follows the symlink to
//    resolve the file but watches by real path, so without this an edit in
//    packages/ui/src would not trigger a reload.
config.watchFolders = [
  path.resolve(workspaceRoot, "packages", "ui"),
  path.resolve(workspaceRoot, "packages", "protocol"),
]

// 2. Resolve from the app's own node_modules first. The repo root is kept as a fallback
//    so a stray hoisted copy still resolves rather than hard-failing.
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, "node_modules"),
  path.resolve(workspaceRoot, "node_modules"),
]

// 3. The shared packages are `"type": "module"` with `main` pointing at a .ts file, so
//    package-exports resolution has to be on for `@nesqbot/*` (and modern deps) to load.
config.resolver.unstable_enablePackageExports = true
config.resolver.unstable_conditionNames = ["require", "import", "react-native", "default"]

// 4. Pin the single copy of the React runtime. The symlinked packages live outside this
//    app's node_modules, so without this a second React could be pulled in through the
//    repo-root fallback above — the classic "invalid hook call" crash.
config.resolver.extraNodeModules = {
  ...config.resolver.extraNodeModules,
  react: path.resolve(projectRoot, "node_modules", "react"),
  "react-native": path.resolve(projectRoot, "node_modules", "react-native"),
}

module.exports = config
