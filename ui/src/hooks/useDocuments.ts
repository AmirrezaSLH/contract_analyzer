import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, newTrace, upload, type UploadOut } from "../api/client";
import { keys } from "./keys";

/** The library table and the sidebar list, from one call. `pages`, `chunks`
 *  and `last_analysis` all arrive on the row, so there is no N+1 here. */
export function useDocuments() {
  return useQuery({ queryKey: keys.documents, queryFn: api.documents });
}

/** One document's own record. The sidebar's meta line, and where
 *  `last_analysis.analysis_id` comes from. */
export function useDocument(id: number | null) {
  return useQuery({
    queryKey: keys.document(id ?? 0),
    queryFn: () => api.document(id!),
    enabled: id !== null,
    retry: (count, error) =>
      // A 404 is an answer, not a failure: the document was deleted, and the
      // view renders `document_not_found`. Retrying it three times only delays
      // that by a second and a half.
      count < 2 && !(error instanceof Error && error.name === "ApiError"),
  });
}

export interface UploadState {
  file: File;
  fraction: number;
}

export function useUpload(onProgress?: (state: UploadState) => void) {
  const client = useQueryClient();
  return useMutation<UploadOut & { traceId: string }, Error, File>({
    mutationFn: async (file) => {
      // One trace id for the whole action, minted here and kept: it is what a
      // reviewer greps `.run/app.jsonl` for.
      const traceId = newTrace();
      const result = await upload(file, {
        traceId,
        onProgress: (fraction) => onProgress?.({ file, fraction }),
      });
      return { ...result, traceId };
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.documents });
    },
  });
}

export function useDeleteDocument() {
  const client = useQueryClient();
  return useMutation<void, Error, number>({
    mutationFn: (id) => api.deleteDocument(id, newTrace()),
    onSuccess: (_result, id) => {
      void client.invalidateQueries({ queryKey: keys.documents });
      client.removeQueries({ queryKey: keys.document(id) });
    },
  });
}
