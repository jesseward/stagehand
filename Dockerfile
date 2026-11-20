# Build:  docker build --build-arg VERSION=$(git describe --tags) -t stagehand .
# Run:    docker run -d -u $(id -u):$(id -g) --net=host \
#           -v $HOME:/stagehand -v /data/tv:/stagehand/tv stagehand
#
# ffprobe (from ffmpeg) is used to verify downloads (resolution, duration,
# audio language).  It's optional; build with --build-arg FFMPEG=0 for a much
# smaller image that skips verification.

FROM python:3.15-slim AS build
# The version normally comes from git tags, but .git isn't in the build context.
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=$VERSION
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.15-slim
ARG FFMPEG=1
RUN if [ "$FFMPEG" = 1 ]; then \
      apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*; \
    fi
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

ENV HOME=/stagehand PYTHONUNBUFFERED=1
VOLUME /stagehand
EXPOSE 8088
ENTRYPOINT ["stagehand"]
