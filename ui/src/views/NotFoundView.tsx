import { useNavigate } from "react-router-dom";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";

export function NotFoundView() {
  const navigate = useNavigate();
  return (
    <EmptyState
      title="There is nothing at this address."
      body="Upload, the library, analysis, chat, operations, and the live log."
      action={
        <Button variant="primary" size="lg" onClick={() => navigate("/library")}>
          Back to library
        </Button>
      }
    />
  );
}
