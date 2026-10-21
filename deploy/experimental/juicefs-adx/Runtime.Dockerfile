# Functional fixture using the same Execd archive as the pinned ADX release.
FROM docker.io/library/ubuntu:24.04 AS rootfs
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgcc-s1 tini && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /var/task /__adx && ln -sfn /home /__adx/home && ln -sfn /usr /__adx/usr && ln -sfn /opt /__adx/opt && ln -sfn /root /__adx/root && test -x /usr/bin/tini-static
COPY adx-execd /usr/local/bin/adx-execd
FROM docker.io/library/ubuntu:24.04 AS pack
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends erofs-utils && rm -rf /var/lib/apt/lists/*
COPY --from=rootfs / /rootfs
RUN mkfs.erofs -E noinline_data /rootfs.erofs /rootfs && fsck.erofs /rootfs.erofs
FROM scratch
COPY --from=pack /rootfs.erofs /rootfs.erofs

