FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS build
RUN apk add --no-cache gcc musl-dev
COPY exec.c sample.c /src/
RUN gcc -static -Os -s -Wall -Wextra -Werror -o /aims-exec /src/exec.c \
 && gcc -static -O2 -s -Wall -Wextra -Werror -o /aims-sample /src/sample.c

FROM busybox:1.37-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092
COPY --from=build /aims-exec /aims-sample /aims/
RUN cp /bin/busybox /aims/busybox
