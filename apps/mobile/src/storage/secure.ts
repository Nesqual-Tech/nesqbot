/**
 * Thin wrapper over expo-secure-store.
 *
 * SecureStore throws on unsupported platforms (web) and can throw on a corrupted
 * keychain entry; every call here degrades to an in-memory map so the app never
 * crashes on a storage failure -- the user just gets signed out on next launch.
 */
import * as SecureStore from "expo-secure-store"
import { Platform } from "react-native"

const memory = new Map<string, string>()

const supported = Platform.OS === "ios" || Platform.OS === "android"

export async function getItem(key: string): Promise<string | null> {
  if (!supported) return memory.get(key) ?? null
  try {
    return await SecureStore.getItemAsync(key)
  } catch {
    return memory.get(key) ?? null
  }
}

export async function setItem(key: string, value: string): Promise<void> {
  memory.set(key, value)
  if (!supported) return
  try {
    await SecureStore.setItemAsync(key, value)
  } catch {
    /* keep the in-memory copy for this session */
  }
}

export async function removeItem(key: string): Promise<void> {
  memory.delete(key)
  if (!supported) return
  try {
    await SecureStore.deleteItemAsync(key)
  } catch {
    /* nothing else we can do */
  }
}
