export const ARTIFACT_LABELS: Record<string, string> = {
  markdown: "Markdown",
  docling_json: "Docling JSON",
  doctags: "DocTags",
}

export function labelFor(key: string): string {
  return ARTIFACT_LABELS[key] ?? key
}
