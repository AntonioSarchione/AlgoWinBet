export default function Loading() {
  return (
    <div aria-busy="true" aria-label="Caricamento" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div className="skeleton" style={{ height: 70 }} />
      <div className="skeleton" style={{ height: 150 }} />
      <div className="skeleton" style={{ height: 320 }} />
    </div>
  );
}
