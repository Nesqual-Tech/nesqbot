/**
 * Attachments on a message, and the chips for ones about to be sent.
 *
 * The bytes are not in the transcript payload (`GET /messages` lists name,
 * type and size only), so an image is fetched on its own — with the session
 * header, which is why this is not a bare `<img src>`: the API is
 * bearer-authenticated and a plain image request carries no token. The blob
 * URL is cached per attachment for the life of the page so scrolling a long
 * thread does not refetch every screenshot.
 */
import { useEffect, useState } from "react"
import { messageAttachments, type MessageAttachment } from "@nesqbot/protocol"
import { fetchAttachment } from "../api/endpoints"
import { formatBytes, isImageType, type StagedAttachment } from "../lib/attachments"
import { useToast } from "../state/AppState"
import { Icon } from "./Icon"
import type { Message } from "../types"

const blobUrls = new Map<string, Promise<string>>()

function cachedBlobUrl(threadId: string, messageId: string, index: number): Promise<string> {
  const key = `${threadId}/${messageId}/${index}`
  let pending = blobUrls.get(key)
  if (!pending) {
    pending = fetchAttachment(threadId, messageId, index).then((blob) => URL.createObjectURL(blob))
    pending.catch(() => blobUrls.delete(key))
    blobUrls.set(key, pending)
  }
  return pending
}

function AttachedImage({
  threadId,
  messageId,
  index,
  attachment,
}: {
  threadId: string
  messageId: string
  index: number
  attachment: MessageAttachment
}) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let live = true
    cachedBlobUrl(threadId, messageId, index)
      .then((next) => live && setUrl(next))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [threadId, messageId, index])

  if (failed) {
    return (
      <span className="attachment attachment--file attachment--broken" title="Could not load this image">
        <Icon name="alert" size={13} />
        {attachment.name}
      </span>
    )
  }
  if (!url) return <span className="attachment attachment--loading" aria-label={`Loading ${attachment.name}`} />
  return (
    <>
      <button
        type="button"
        className="attachment attachment--image"
        onClick={() => setOpen(true)}
        title={attachment.name}
      >
        <img src={url} alt={attachment.name} loading="lazy" />
      </button>
      {open ? (
        <div className="lightbox" role="dialog" aria-modal="true" aria-label={attachment.name}>
          <button type="button" className="lightbox__backdrop" aria-label="Close" onClick={() => setOpen(false)} />
          <img className="lightbox__image" src={url} alt={attachment.name} />
          <span className="lightbox__caption">
            {attachment.name} · {formatBytes(attachment.size)}
          </span>
        </div>
      ) : null}
    </>
  )
}

function AttachedFile({
  threadId,
  messageId,
  index,
  attachment,
}: {
  threadId: string
  messageId: string
  index: number
  attachment: MessageAttachment
}) {
  const toast = useToast()
  const open = async () => {
    try {
      const url = await cachedBlobUrl(threadId, messageId, index)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = attachment.name
      anchor.rel = "noopener"
      anchor.click()
    } catch (err) {
      toast.error("Could not fetch the file", err instanceof Error ? err.message : undefined)
    }
  }
  return (
    <button type="button" className="attachment attachment--file" onClick={() => void open()} title="Save this file">
      <Icon name="file" size={13} />
      <span className="attachment__name">{attachment.name}</span>
      <span className="attachment__size">{formatBytes(attachment.size)}</span>
    </button>
  )
}

/** The files on one message. Renders nothing when there are none. */
export function MessageAttachments({ message }: { message: Message }) {
  const items = messageAttachments(message)
  if (!items.length) return null
  // An optimistic bubble has a local id the API does not know; its previews
  // are carried on the message itself until the transcript refetches.
  const previews = (message as Message & { _previews?: Array<string | null> })._previews
  return (
    <div className="attachments" aria-label="Attachments">
      {items.map((attachment, index) => {
        const preview = previews?.[index]
        if (preview) {
          return (
            <span key={index} className="attachment attachment--image" title={attachment.name}>
              <img src={preview} alt={attachment.name} />
            </span>
          )
        }
        if (previews) {
          return (
            <span key={index} className="attachment attachment--file">
              <Icon name="file" size={13} />
              <span className="attachment__name">{attachment.name}</span>
            </span>
          )
        }
        return isImageType(attachment.media_type) ? (
          <AttachedImage
            key={index}
            threadId={message.thread_id}
            messageId={message.id}
            index={index}
            attachment={attachment}
          />
        ) : (
          <AttachedFile
            key={index}
            threadId={message.thread_id}
            messageId={message.id}
            index={index}
            attachment={attachment}
          />
        )
      })}
    </div>
  )
}

/** Chips for the files staged in the composer, each with a remove control. */
export function StagedAttachments({ items, onRemove }: { items: StagedAttachment[]; onRemove: (uid: string) => void }) {
  if (!items.length) return null
  return (
    <div className="composer__attachments" aria-label="Files to send">
      {items.map((item) => (
        <span key={item.uid} className={item.previewUrl ? "staged staged--image" : "staged"}>
          {item.previewUrl ? <img src={item.previewUrl} alt="" /> : <Icon name="file" size={13} />}
          <span className="staged__name" title={item.upload.name}>
            {item.upload.name}
          </span>
          <span className="staged__size">{formatBytes(item.size)}</span>
          <button
            type="button"
            className="staged__remove"
            aria-label={`Remove ${item.upload.name}`}
            onClick={() => onRemove(item.uid)}
          >
            <Icon name="close" size={11} />
          </button>
        </span>
      ))}
    </div>
  )
}
