import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { keys } from "./keys";

/**
 * What this deployment is, read once at boot.
 *
 * Everything a client would otherwise hardcode is here: the upload limit, the
 * model allowlist, the retrieval defaults, the pool shape, and `key_present`
 * -- which is what gates the `no_api_key` banner *before* a user clicks a
 * button that cannot work, rather than three clicks later.
 */
export function useHealth() {
  return useQuery({
    queryKey: keys.health,
    queryFn: api.health,
    // Configuration, not data. It changes when the process restarts, and a
    // reload is how you find out.
    staleTime: 5 * 60_000,
    retry: 1,
  });
}
