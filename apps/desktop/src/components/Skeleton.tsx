import { cx } from "../lib/format"

export interface SkeletonProps {
  width?: string
  height?: string
  radius?: "sm" | "md" | "lg" | "pill"
  className?: string
}

/**
 * Loading placeholder. Width/height are layout, not colour — the shimmer comes
 * entirely from `--skeleton` tokens in `styles.css`.
 */
export function Skeleton({ width = "100%", height = "12px", radius = "sm", className }: SkeletonProps) {
  return (
    <span className={cx("skeleton", `skeleton--${radius}`, className)} style={{ width, height }} aria-hidden="true" />
  )
}

export function SkeletonList({ rows = 3, gap = "10px" }: { rows?: number; gap?: string }) {
  return (
    <div className="skeleton-list" style={{ gap }} aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-row" key={index}>
          <Skeleton width="32px" height="32px" radius="md" />
          <div className="skeleton-row__lines">
            <Skeleton width="60%" height="10px" />
            <Skeleton width="40%" height="8px" />
          </div>
        </div>
      ))}
    </div>
  )
}

export function SkeletonCards({ cards = 2 }: { cards?: number }) {
  return (
    <div className="skeleton-cards" aria-hidden="true">
      {Array.from({ length: cards }, (_, index) => (
        <div className="card" key={index}>
          <Skeleton width="45%" height="14px" />
          <Skeleton width="80%" height="10px" />
          <Skeleton width="65%" height="10px" />
        </div>
      ))}
    </div>
  )
}
