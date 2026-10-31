import { useQuery } from "@tanstack/react-query";

import { whoami } from "@/api/session";

export function useOperator(): boolean {
  const me = useQuery(whoami);
  return me.data?.role === "operator";
}
