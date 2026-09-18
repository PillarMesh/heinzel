export function LoadingPage() {
  return (
    <main aria-label="Heinzel console" className="standalone-state">
      <div aria-live="polite" className="loading-state" role="status">
        <span aria-hidden="true" className="loading-state__mark" />
        Loading governed workspace
      </div>
    </main>
  )
}
