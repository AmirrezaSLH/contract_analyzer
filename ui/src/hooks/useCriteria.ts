import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { keys } from "./keys";

/** The five questions and their sub-requirements. Static for the process, so
 *  it is fetched once and never refetched -- and it is what supplies the *full
 *  requirement text* in an expanded criterion row, because `GOV-04` does not
 *  say what was checked. */
export function useCriteria() {
  return useQuery({ queryKey: keys.criteria, queryFn: api.criteria, staleTime: Infinity });
}
