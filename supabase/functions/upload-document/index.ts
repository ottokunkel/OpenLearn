import { createClient } from "npm:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
      },
    });
  }

  if (req.method !== "POST") {
    return Response.json({ error: "Method not allowed" }, { status: 405 });
  }

  // 1. Authenticate user
  const authHeader = req.headers.get("Authorization");
  if (!authHeader) {
    return Response.json({ error: "Missing authorization header" }, { status: 401 });
  }

  const userClient = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    global: { headers: { Authorization: authHeader } },
  });

  const { data: { user }, error: authError } = await userClient.auth.getUser();
  if (authError || !user) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  // 2. Parse multipart form data
  let formData: FormData;
  try {
    formData = await req.formData();
  } catch {
    return Response.json({ error: "Expected multipart/form-data with a 'file' field" }, { status: 400 });
  }

  const file = formData.get("file");
  if (!file || !(file instanceof File)) {
    return Response.json({ error: "Missing 'file' field" }, { status: 400 });
  }

  if (!file.name.toLowerCase().endsWith(".pdf")) {
    return Response.json({ error: "Only PDF files are supported" }, { status: 400 });
  }

  if (file.size > MAX_FILE_SIZE) {
    return Response.json(
      { error: `File too large. Maximum size is ${MAX_FILE_SIZE / 1024 / 1024} MB` },
      { status: 400 },
    );
  }

  // Parse optional metadata from query params
  const url = new URL(req.url);
  let userMetadata: Record<string, unknown> = {};
  const metadataParam = url.searchParams.get("metadata");
  if (metadataParam) {
    try {
      userMetadata = JSON.parse(metadataParam);
    } catch {
      return Response.json({ error: "Invalid metadata JSON" }, { status: 400 });
    }
  }

  // 3. Upload file to Supabase Storage
  const documentId = crypto.randomUUID();
  const filePath = `${user.id}/${documentId}/${file.name}`;

  const adminClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

  const { error: uploadError } = await adminClient.storage
    .from("documents")
    .upload(filePath, file, {
      contentType: "application/pdf",
      upsert: false,
    });

  if (uploadError) {
    return Response.json({ error: `Upload failed: ${uploadError.message}` }, { status: 500 });
  }

  // 4. Create document record (as user — satisfies RLS INSERT policy)
  const metadata = {
    ...userMetadata,
    original_filename: file.name,
    file_size_bytes: file.size,
    mime_type: "application/pdf",
    uploaded_at: new Date().toISOString(),
  };

  const { error: insertError } = await userClient
    .from("documents")
    .insert({
      id: documentId,
      user_id: user.id,
      filename: file.name,
      file_path: filePath,
      status: "pending",
      metadata,
    });

  if (insertError) {
    // Clean up uploaded file
    await adminClient.storage.from("documents").remove([filePath]);
    return Response.json({ error: `Failed to create document: ${insertError.message}` }, { status: 500 });
  }

  // 5. Enqueue job via pgmq (service role — SECURITY DEFINER function)
  const { error: queueError } = await adminClient.rpc("enqueue_document_job", {
    p_document_id: documentId,
    p_user_id: user.id,
    p_file_path: filePath,
    p_filename: file.name,
    p_metadata: metadata,
  });

  if (queueError) {
    // Update document status to failed
    await adminClient
      .from("documents")
      .update({ status: "failed", error_message: `Queue error: ${queueError.message}` })
      .eq("id", documentId);
    return Response.json({ error: `Failed to enqueue job: ${queueError.message}` }, { status: 500 });
  }

  // 6. Return success
  return Response.json(
    { document_id: documentId, status: "pending" },
    {
      status: 201,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
      },
    },
  );
});
