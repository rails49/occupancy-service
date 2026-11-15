# The occupancy service, with OpenVINO, and on amd64 the Intel compute runtime
# for the GPU. The build context is the repository root.
#
# DETECTOR_VERSION installs that version of rails49-detector (AGPL) from PyPI,
# with its license text and source URL in /usr/share/doc/rails49-detector.
# Set empty, the image holds no detector code.

FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

# ffmpeg with only what capture.py runs: MJPEG from a V4L2 device, or over HTTP
# or RTSP, copied to MJPEG on stdout. It is LGPL, with its license text and
# source URL in /usr/share/doc/ffmpeg.
FROM ubuntu:24.04 AS ffmpeg

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gcc libc6-dev make xz-utils \
    && rm -rf /var/lib/apt/lists/*

ARG FFMPEG_VERSION=9.0.2
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
WORKDIR /src
RUN curl -fsSLo ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz" \
    && echo "$FFMPEG_SHA256  ffmpeg.tar.xz" | sha256sum -c - \
    && tar -xJf ffmpeg.tar.xz --strip-components=1 \
    && ./configure --disable-everything --disable-autodetect --disable-doc \
    --disable-ffplay --disable-ffprobe --disable-x86asm \
    --enable-indev=v4l2 --enable-demuxer=mjpeg,mpjpeg,rtsp \
    --enable-protocol=http,pipe,tcp --enable-decoder=mjpeg \
    --enable-parser=mjpeg --enable-bsf=mjpeg2jpeg --enable-muxer=mjpeg \
    && make -j"$(nproc)" ffmpeg \
    && mkdir -p /out/usr/local/bin /out/usr/share/doc/ffmpeg \
    && install -s ffmpeg /out/usr/local/bin/ \
    && cp COPYING.LGPLv2.1 LICENSE.md /out/usr/share/doc/ffmpeg/ \
    && echo "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz" \
    > /out/usr/share/doc/ffmpeg/SOURCE

FROM ubuntu:24.04

ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    $(if [ "$TARGETARCH" = amd64 ]; then echo intel-opencl-icd ocl-icd-libopencl1; fi) \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /out /

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# uv and its cache are mounted for each step, and stay out of the image.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra openvino --no-install-project

COPY src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra openvino --no-editable

ARG DETECTOR_VERSION=0.0.2
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    if [ -n "$DETECTOR_VERSION" ]; then \
    uv pip install --python /opt/venv/bin/python "rails49-detector==$DETECTOR_VERSION" \
    && mkdir -p /usr/share/doc/rails49-detector \
    && cp /opt/venv/lib/python*/site-packages/rails49_detector-*.dist-info/licenses/LICENSE \
    /usr/share/doc/rails49-detector/ \
    && echo "https://github.com/rails49/detector/tree/v$DETECTOR_VERSION" \
    > /usr/share/doc/rails49-detector/SOURCE; \
    fi

# The container runs as the person's uid, which has no home here.
ENV PATH=/opt/venv/bin:$PATH HOME=/tmp

# This repository's commit, which compose.yaml passes. Empty for a build that
# names none.
ARG OCCUPANCY_SERVICE_COMMIT=
LABEL org.opencontainers.image.revision=$OCCUPANCY_SERVICE_COMMIT

ENTRYPOINT ["python", "-m", "occupancy_service"]
