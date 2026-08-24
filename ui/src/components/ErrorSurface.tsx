import { ApiError } from "../api/client";
import { surfaceFor } from "../api/errors";
import { Banner } from "./Banner";
import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

interface Props {
  error: unknown;
  /** What the recovery action does. Omitted where the table says there is
   *  nothing to retry. */
  onRetry?: () => void;
  /** Overrides the placement the table chose. Used where a view knows better
   *  than the table -- a `document_not_found` inside a delete dialog is inline,
   *  not a full pane. */
  as?: "inline" | "banner";
}

/**
 * One failure, rendered where §4 says it goes.
 *
 * Views pass an error and get a surface. They do not switch on `code`; the
 * table does that once, so a new code is one row there and no change here.
 */
export function ErrorSurface({ error, onRetry, as }: Props) {
  if (!(error instanceof ApiError)) {
    // Not from the API at all -- a bug in this front end. Rendered rather than
    // swallowed, because a blank pane says nothing and a thrown error in a
    // render is a white screen.
    return (
      <Banner
        tone="error"
        title="Something in this page went wrong."
        hint={error instanceof Error ? error.message : String(error)}
      />
    );
  }
  const surface = surfaceFor(error);
  const action =
    surface.retry && onRetry ? (
      <Button variant="secondary" size="sm" onClick={onRetry}>
        {surface.retry}
      </Button>
    ) : null;

  if ((as ?? surface.placement) === "full-pane") {
    return <EmptyState title={surface.title} body={surface.hint} action={action} />;
  }
  return (
    <Banner
      tone={surface.placement === "banner" ? "warn" : "error"}
      title={surface.title}
      body={surface.body}
      hint={surface.hint}
      traceId={surface.traceId}
      action={action}
    />
  );
}
