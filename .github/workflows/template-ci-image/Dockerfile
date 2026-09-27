# check=skip=InvalidDefaultArgInFrom
# Linux CI image of the TEMPLATE repository (template-ci-image.yml; `./pyt new` never copies
# .github/workflows/template-*). No version is written here: they come from pins.py, whose `env`
# output the build script puts next to this file as pins.env. Build it with `sh build`, never
# `docker build` alone: the context is the template's tracked files, prepared by that script.
# BASE has no default on purpose: the build script passes pins.py's
ARG BASE
FROM ${BASE}
ARG SOURCE_URL
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.description="Linux CI image of py_template: the pinned tools and warm caches of its test suites"
# the system layer first: it changes only with the pins, not with an edit of warm or the lock
COPY .github/workflows/template-ci-image/pins.env .github/workflows/template-ci-image/system /opt/ci/
RUN --mount=type=secret,id=ca,mode=0444 sh /opt/ci/system
COPY .github/workflows/template-ci-image/ /opt/ci/
USER runner
ENV HOME=/home/runner \
    LANG=C.UTF-8 \
    CI_IMAGE_INPUTS=/opt/ci/inputs.txt \
    PATH=/opt/ci/uv/bin:/opt/ci/nvim/default/bin:/home/runner/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/runner
# One layer per step: a pull downloads the layers in parallel
RUN --mount=type=bind,source=.,target=/src --mount=type=secret,id=ca,mode=0444 sh /opt/ci/warm template
RUN --mount=type=bind,source=.,target=/src --mount=type=secret,id=ca,mode=0444 sh /opt/ci/warm presets
CMD ["sleep", "infinity"]
