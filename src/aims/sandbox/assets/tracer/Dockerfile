FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
RUN apt-get update \
 && apt-get install -y --no-install-recommends bpftrace \
 && rm -rf /var/lib/apt/lists/*
COPY trace.bt /aims/trace.bt
CMD ["sh", "-c", "mountpoint -q /sys/kernel/tracing || mount -t tracefs nodev /sys/kernel/tracing; exec bpftrace -B line -f json /aims/trace.bt"]
