import { useEffect, useRef } from "react";

// A <details> that opens when `cond` turns true and is otherwise left as the person set it;
// an `open` prop would fold it shut the moment `cond` turns false, even mid-typing.
export function useOpenWhen(cond: boolean) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (cond && ref.current) ref.current.open = true;
  }, [cond]);
  return ref;
}
