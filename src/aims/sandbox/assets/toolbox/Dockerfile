FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS build
RUN apk add --no-cache gcc linux-headers musl-dev
COPY exec.c sample.c snap.c /src/
RUN gcc -static -Os -s -Wall -Wextra -Werror -o /aims-exec /src/exec.c \
 && gcc -static -O2 -s -Wall -Wextra -Werror -o /aims-sample /src/sample.c \
 && gcc -static -Os -s -Wall -Wextra -Werror -o /aims-snap /src/snap.c

FROM busybox:1.38-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6
COPY --from=build /aims-exec /aims-sample /aims/
COPY --from=build /aims-snap /reader/aims-snap
RUN cp /bin/busybox /aims/busybox
