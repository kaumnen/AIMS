FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
RUN apt-get update \
 && apt-get install -y --no-install-recommends bpftrace \
 && rm -rf /var/lib/apt/lists/*
ENV BPFTRACE_PERF_RB_PAGES=16384
COPY trace.bt /aims/trace.bt
CMD ["sleep", "infinity"]
