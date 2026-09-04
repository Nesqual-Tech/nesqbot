/**
 * Files on their way into a message.
 *
 * The limits mirror `app/services/attachments.py` on the API so a person is
 * told "too big" before the upload, not after a 400 — but the API's check is
 * the one that counts, and this file never assumes it will not be refused.
 */
import type { AttachmentUpload } from "@nesqbot/protocol"

export const MAX_ATTACHMENTS = 4
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024
export const MAX_TEXT_BYTES = 256 * 1024

export const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"])
export const TEXT_TYPES = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/tab-separated-values",
  "application/json",
  "text/x-log",
])

/** What the file picker offers. Matches the two accepted families. */
export const ACCEPT = [...IMAGE_TYPES, ...TEXT_TYPES, ".txt", ".md", ".csv", ".tsv", ".json", ".log"].join(",")

/**
 * A file staged in the composer: what is sent, plus what is shown.
 * `previewUrl` is an object URL for images and must be revoked on removal.
 */
export interface StagedAttachment {
  uid: string
  upload: AttachmentUpload
  size: number
  previewUrl: string | null
}

export function isImageType(mediaType: string): boolean {
  return IMAGE_TYPES.has(mediaType)
}

/**
 * Browsers leave `File.type` empty for a `.md` or `.log`, and report `.csv`
 * as `application/vnd.ms-excel` on Windows when Excel is installed. The
 * extension is the more honest signal for text; the sniffed type wins for
 * images because a renamed JPEG is still a JPEG.
 */
export function mediaTypeFor(file: File): string {
  const declared = (file.type || "").split(";")[0].trim().toLowerCase()
  if (declared === "image/jpg") return "image/jpeg"
  if (IMAGE_TYPES.has(declared)) return declared
  const ext = file.name.toLowerCase().split(".").pop() ?? ""
  const byExt: Record<string, string> = {
    txt: "text/plain",
    text: "text/plain",
    log: "text/x-log",
    md: "text/markdown",
    markdown: "text/markdown",
    csv: "text/csv",
    tsv: "text/tab-separated-values",
    json: "application/json",
  }
  if (byExt[ext]) return byExt[ext]
  if (TEXT_TYPES.has(declared)) return declared
  return declared
}

export class AttachmentRejected extends Error {
  constructor(
    readonly file: File,
    message: string,
  ) {
    super(message)
    this.name = "AttachmentRejected"
  }
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error ?? new Error("Could not read the file"))
    reader.onload = () => {
      const result = String(reader.result ?? "")
      const at = result.indexOf(";base64,")
      resolve(at >= 0 ? result.slice(at + ";base64,".length) : result)
    }
    reader.readAsDataURL(file)
  })
}

/** Read one file into a staged attachment, or throw `AttachmentRejected`. */
export async function stageFile(file: File): Promise<StagedAttachment> {
  const mediaType = mediaTypeFor(file)
  if (!IMAGE_TYPES.has(mediaType) && !TEXT_TYPES.has(mediaType)) {
    throw new AttachmentRejected(file, `${file.name}: only images and text files (txt, md, csv, json) can be attached`)
  }
  const limit = IMAGE_TYPES.has(mediaType) ? MAX_IMAGE_BYTES : MAX_TEXT_BYTES
  if (file.size > limit) {
    throw new AttachmentRejected(file, `${file.name} is ${formatBytes(file.size)}; the limit is ${formatBytes(limit)}`)
  }
  if (file.size === 0) throw new AttachmentRejected(file, `${file.name} is empty`)
  const data = await toBase64(file)
  return {
    uid: `att-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    upload: { name: file.name || (IMAGE_TYPES.has(mediaType) ? "image.png" : "file.txt"), media_type: mediaType, data },
    size: file.size,
    previewUrl: IMAGE_TYPES.has(mediaType) ? URL.createObjectURL(file) : null,
  }
}

export function releaseStaged(staged: StagedAttachment): void {
  if (staged.previewUrl) URL.revokeObjectURL(staged.previewUrl)
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * Files out of a paste or a drop. A pasted screenshot arrives as a `File`
 * named `image.png` with no path; a dropped folder arrives as nothing useful
 * and is skipped.
 */
export function filesFrom(transfer: DataTransfer | null | undefined): File[] {
  if (!transfer) return []
  const out: File[] = []
  if (transfer.items && transfer.items.length) {
    for (const item of Array.from(transfer.items)) {
      if (item.kind !== "file") continue
      const file = item.getAsFile()
      if (file) out.push(file)
    }
    if (out.length) return out
  }
  return Array.from(transfer.files ?? [])
}
