import { Navigate } from "react-router-dom";
import { useDocuments } from "../hooks/useDocuments";

/** `/` is not a page. It is the library if there is anything in it, and the
 *  drop zone if there is not -- because an empty library is a worse first
 *  screen than the thing that fills it. */
export function RootRedirect() {
  const documents = useDocuments();
  if (documents.isPending) return null;
  const any = (documents.data?.length ?? 0) > 0;
  return <Navigate to={any ? "/library" : "/upload"} replace />;
}
