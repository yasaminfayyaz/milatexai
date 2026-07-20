# MiLatexAI hosted server — Streamable HTTP MCP at /mcp/
FROM python:3.12-slim

# git: the git worker shells out to it. curl: fetch Tectonic at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Tectonic — a self-contained LaTeX engine that fetches TeX packages on demand.
# Far smaller than a TeX Live install (~5 GB), so it suits a scale-to-zero image.
# Pinned static musl build (no shared-lib deps), extracted straight to PATH.
ARG TECTONIC_VERSION=0.16.9
RUN curl -fsSL "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
      | tar -xz -C /usr/local/bin tectonic \
    && chmod +x /usr/local/bin/tectonic \
    && tectonic --version

# latexdiff (self-contained perl script; slim's perl-base suffices): the
# tracked-changes PDF feature. Verified at build.
ARG LATEXDIFF_VERSION=1.4.0
RUN curl -fsSL "https://github.com/ftilmann/latexdiff/releases/download/${LATEXDIFF_VERSION}/latexdiff-${LATEXDIFF_VERSION}.tar.gz" \
      | tar -xz -C /tmp \
    && cp /tmp/latexdiff/latexdiff-so /usr/local/bin/latexdiff \
    && chmod +x /usr/local/bin/latexdiff \
    && rm -rf /tmp/latexdiff \
    && latexdiff --version

# Warm the package-bundle cache into an image layer, so the FIRST compile after a
# cold start needs no network. (Runtime downloads of un-primed packages still work,
# they're just not persisted past a scale-to-zero.)
ENV TECTONIC_CACHE_DIR=/opt/tectonic-cache
COPY docker/prime.tex /tmp/prime/main.tex
RUN cd /tmp/prime && tectonic -X compile main.tex && rm -rf /tmp/prime

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY leafbridge/ ./leafbridge/
COPY pyproject.toml README.md ./

# Disposable clone cache on the container's writable layer.
ENV LEAFBRIDGE_DATA_DIR=/tmp/mila-cache \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "leafbridge.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
