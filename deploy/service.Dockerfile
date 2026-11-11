# The occupancy service, with OpenVINO, and on amd64 the Intel compute runtime
# for the GPU. The build context is the repository root.
#
# DETECTOR_VERSION, empty by default, installs that version of rails49-detector
# (AGPL) from PyPI. Empty, the image holds no detector code.

FROM ubuntu:24.04

ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg \
    $(if [ "$TARGETARCH" = amd64 ]; then echo intel-opencl-icd ocl-icd-libopencl1; fi) \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-dev --extra openvino --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --extra openvino --no-editable

ARG DETECTOR_VERSION=
RUN if [ -n "$DETECTOR_VERSION" ]; then \
    uv pip install --python /opt/venv/bin/python "rails49-detector==$DETECTOR_VERSION"; \
    fi

# The container runs as the person's uid, which has no home here.
ENV PATH=/opt/venv/bin:$PATH HOME=/tmp

ENTRYPOINT ["python", "-m", "occupancy_service"]
