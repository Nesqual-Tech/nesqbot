// babel-preset-expo already wires up expo-router (the standalone `expo-router/babel`
// plugin was removed in SDK 50) and the React Native JSX runtime.
module.exports = function babelConfig(api) {
  api.cache(true)
  return {
    presets: [["babel-preset-expo", { jsxImportSource: "react" }]],
  }
}
