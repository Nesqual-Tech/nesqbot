/**
 * The shared knowledge base — curated here, read by every bot's RAG search.
 *
 * `GET/POST/PATCH/DELETE /kb` have been wired in `api/endpoints.ts` since the
 * v0.3 contract; nothing in the app rendered them. An article a support bot
 * cites in a reply could only be written, read or fixed with `curl`. What a
 * bot actually knows is worth a screen of its own, not just a search
 * endpoint nobody outside the API can reach.
 */
import { useCallback, useEffect, useState } from "react"
import { errorMessage } from "../api/client"
import { createKbArticle, deleteKbArticle, searchKb, updateKbArticle } from "../api/endpoints"
import { relativeTime, truncate } from "../lib/format"
import { useToast } from "../state/AppState"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { KbArticle } from "../types"

const EMPTY_DRAFT = { title: "", body: "" }

function ArticleEditor({
  initial,
  onSave,
  onCancel,
  saveLabel,
}: {
  initial: { title: string; body: string }
  onSave: (values: { title: string; body: string }) => Promise<void>
  onCancel?: () => void
  saveLabel: string
}) {
  const [title, setTitle] = useState(initial.title)
  const [body, setBody] = useState(initial.body)
  const [saving, setSaving] = useState(false)
  const canSave = title.trim().length > 0 && body.trim().length > 0

  return (
    <div className="card kb-editor">
      <label className="field">
        <span className="field__label">Title</span>
        <input className="input" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Password reset" />
      </label>
      <label className="field">
        <span className="field__label">Body</span>
        <textarea
          className="input"
          rows={6}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          placeholder="What a bot should say when this comes up…"
        />
        <span className="field__hint">Grounds a bot's reply with a citation — see it come back scored in search below once saved.</span>
      </label>
      <div className="row-actions">
        <button
          type="button"
          className="btn btn--primary btn--sm"
          disabled={!canSave || saving}
          onClick={async () => {
            setSaving(true)
            try {
              await onSave({ title: title.trim(), body: body.trim() })
            } finally {
              setSaving(false)
            }
          }}
        >
          {saving ? <Spinner inline label="Saving" /> : saveLabel}
        </button>
        {onCancel ? (
          <button type="button" className="btn btn--ghost btn--sm" onClick={onCancel} disabled={saving}>
            Cancel
          </button>
        ) : null}
      </div>
    </div>
  )
}

function ArticleCard({
  article,
  onUpdate,
  onDelete,
}: {
  article: KbArticle
  onUpdate: (id: string, values: { title: string; body: string }) => Promise<void>
  onDelete: (id: string) => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  if (editing) {
    return (
      <ArticleEditor
        initial={{ title: article.title, body: article.body }}
        saveLabel="Save changes"
        onCancel={() => setEditing(false)}
        onSave={async (values) => {
          await onUpdate(article.id, values)
          setEditing(false)
        }}
      />
    )
  }

  return (
    <article className="card kb-card">
      <header className="kb-card__header">
        <h3 className="kb-card__title">{article.title}</h3>
        <span className="kb-card__time">{relativeTime(article.created_at)}</span>
      </header>
      <p className="kb-card__body">{truncate(article.body, 220)}</p>
      <div className="row-actions">
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => setEditing(true)}>
          Edit
        </button>
        {confirmDelete ? (
          <>
            <button
              type="button"
              className="btn btn--danger btn--sm"
              onClick={() => void onDelete(article.id)}
            >
              Confirm delete
            </button>
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirmDelete(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirmDelete(true)}>
            Delete
          </button>
        )}
      </div>
    </article>
  )
}

export function KnowledgePanel() {
  const toast = useToast()
  const [articles, setArticles] = useState<KbArticle[]>([])
  const [query, setQuery] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [creating, setCreating] = useState(false)

  const load = useCallback(async (q: string) => {
    const result = await searchKb(q.trim() || "", 50)
    setArticles(result)
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    const timer = window.setTimeout(() => {
      load(query)
        .catch((err) => !cancelled && setError(err))
        .finally(() => !cancelled && setLoading(false))
    }, query ? 250 : 0) // debounce a live search, but not the initial load
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const refresh = () => load(query).catch((err) => setError(err))

  const onCreate = async (values: { title: string; body: string }) => {
    try {
      const article = await createKbArticle(values)
      toast.success("Article added", article.title)
      setCreating(false)
      await refresh()
    } catch (err) {
      toast.error("Could not add the article", errorMessage(err))
    }
  }

  const onUpdate = async (id: string, values: { title: string; body: string }) => {
    try {
      await updateKbArticle(id, values)
      toast.success("Article updated", values.title)
      await refresh()
    } catch (err) {
      toast.error("Could not update the article", errorMessage(err))
    }
  }

  const onDelete = async (id: string) => {
    try {
      await deleteKbArticle(id)
      setArticles((current) => current.filter((a) => a.id !== id))
      toast.success("Article removed")
    } catch (err) {
      toast.error("Could not remove the article", errorMessage(err))
    }
  }

  return (
    <section className="panel" id="panel-knowledge" role="tabpanel" aria-labelledby="nav-tab-knowledge">
      <header className="panel__header">
        <div>
          <div className="eyebrow">Shared</div>
          <h2 className="panel__title">Knowledge</h2>
          <p className="panel__subtitle">What every bot can cite. Vector search when embeddings are configured, keyword match otherwise.</p>
        </div>
        <div className="panel__header-actions">
          <label className="sr-only" htmlFor="kb-search">
            Search the knowledge base
          </label>
          <input
            id="kb-search"
            className="input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search articles…"
          />
          <button type="button" className="btn btn--primary btn--sm" onClick={() => setCreating((prev) => !prev)}>
            <Icon name="plus" size={14} />
            {creating ? "Close" : "New article"}
          </button>
        </div>
      </header>

      <div className="panel__body">
        {creating ? (
          <ArticleEditor initial={EMPTY_DRAFT} saveLabel="Add article" onSave={onCreate} />
        ) : null}

        {loading ? <SkeletonCards cards={3} /> : null}

        {error && articles.length === 0 && !loading ? (
          <ErrorState error={error} title="Knowledge base unavailable" onRetry={refresh} />
        ) : null}

        {!loading && !error && articles.length === 0 ? (
          <EmptyState
            glyph="book"
            title={query ? "No matches" : "No articles yet"}
            description={query ? "Try a different search." : "Add the first article so bots have something to cite."}
          />
        ) : null}

        <div className="panel__body--grid kb-grid">
          {articles.map((article) => (
            <ArticleCard key={article.id} article={article} onUpdate={onUpdate} onDelete={onDelete} />
          ))}
        </div>
      </div>
    </section>
  )
}
