// On paper a link to an event is `seq N` with the URL it opens, as the report Markdown writes
// it. The print CSS in styles.css hides the link's own words and shows these instead. No URL
// when the report has no base URL.
export interface PrintSeq {
  "data-print-seq": number;
  "data-print-url"?: string;
}

export function printSeq(base: string | null, run: string, seq: number): PrintSeq {
  if (base === null) return { "data-print-seq": seq };
  return {
    "data-print-seq": seq,
    "data-print-url": `${base}/runs/${encodeURIComponent(run)}?seq=${seq}`,
  };
}
