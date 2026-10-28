FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==9.1.1
RUN sp="$(python -c 'import site; print(site.getsitepackages()[0])')" \
    && printf 'import site; site.addsitedir("/tmp/aims-site")\n' > "$sp/zz_extra.pth"
