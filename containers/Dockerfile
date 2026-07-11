ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

RUN useradd --create-home --uid 10001 bench
WORKDIR /bench
COPY --chown=bench:bench . /bench
RUN python -m pip install --no-cache-dir .

USER bench
ENTRYPOINT ["basecamp-bench"]
CMD ["--help"]
