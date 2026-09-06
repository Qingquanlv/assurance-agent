ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BUILD_INPUT_DIGEST
LABEL org.assurance.runner.inputs=${BUILD_INPUT_DIGEST}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
COPY runner-requirements.txt /build/runner-requirements.txt
COPY wheels /build/wheels
RUN python -m pip install --no-cache-dir --require-hashes -r /build/runner-requirements.txt && \
    python -m pip install --no-cache-dir --no-index --no-deps /build/wheels/*.whl && \
    python -m pip check && rm -rf /build
USER 65534:65534
WORKDIR /tests
