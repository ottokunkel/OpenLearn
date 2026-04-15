import { supabase, STORAGE_BUCKET } from "@/lib/supabase"

export async function downloadArtifact(storagePath: string): Promise<string> {
  const { data, error } = await supabase.storage
    .from(STORAGE_BUCKET)
    .createSignedUrl(storagePath, 60 * 60) // 1 hour
  if (error || !data?.signedUrl) {
    throw new Error(error?.message ?? "Failed to create signed URL")
  }
  return data.signedUrl
}
