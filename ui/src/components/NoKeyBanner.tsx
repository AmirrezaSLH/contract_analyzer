import { useHealth } from "../hooks/useHealth";
import { Banner } from "./Banner";

/**
 * `no_api_key`, said before the click rather than after it.
 *
 * `/health` reports `key_present` for exactly this reason: analysis and chat
 * need an answer key, upload and retrieval do not, and a product that lets
 * someone press "Run compliance analysis" and *then* explains has wasted their
 * click. The Run button carries the same sentence as its `disabledReason`;
 * this is the half that says what still works.
 */
export function NoKeyBanner() {
  const health = useHealth();
  if (health.data?.key_present !== false) return null;
  return (
    <Banner
      tone="warn"
      title="No answer model is configured, so analysis and chat are unavailable."
      hint="Set ANTHROPIC_API_KEY in .env and restart. Uploading contracts and browsing the library work without it."
    />
  );
}

/** The same sentence, for a control's `disabledReason`. */
export const NO_KEY_REASON =
  "No answer model is configured. Set ANTHROPIC_API_KEY in .env and restart.";
