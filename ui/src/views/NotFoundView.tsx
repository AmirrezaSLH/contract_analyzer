import { useNavigate } from "react-router-dom";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";

export function NotFoundView() {
  const navigate = useNavigate();
  return (
    <EmptyState
      title="There is nothing at this address."
      body="The four pages are upload, the library, and a document's analysis and chat."
      action={
        <Button variant="primary" size="lg" onClick={() => navigate("/library")}>
          Back to library
        </Button>
      }
    />
  );
}
