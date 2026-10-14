# ---------- 构建参数 ----------
ARG PYTHON_VERSION=3.12
# 国内加速：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL
# Docker Hub 直连不通时换镜像源，可选：docker.m.daocloud.io / dockerproxy.net / hub.rat.dev
ARG BASE_REGISTRY=docker.m.daocloud.io

FROM ${BASE_REGISTRY}/library/python:${PYTHON_VERSION}-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5050

# ---------- 依赖层（单独一层，改代码不重装依赖）----------
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} -r requirements.txt

# ---------- edge-tts：直接从仓库源码安装 ----------
COPY src/      /app/pkg/src/
COPY setup.py  /app/pkg/setup.py
COPY setup.cfg /app/pkg/setup.cfg
COPY README.md /app/pkg/README.md
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} /app/pkg \
    && rm -rf /app/pkg/build

# ---------- 服务层 ----------
COPY server.py /app/server.py
COPY static/ /app/static/

# ---------- 更新工具（“一键自动更新/手动下载”功能依赖）----------
# git 用于拉取上游代码；docker CLI 用于调用宿主机 docker.sock 构建/重建容器
ARG DOCKER_VERSION=27.3.1
RUN set -eux; \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g; s|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates curl; \
    for url in \
        "https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
        "https://mirrors.aliyun.com/docker-ce/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
        "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz"; do \
        curl -fsSL "$url" -o /tmp/docker.tgz && break || echo "源失败，尝试下一个: $url"; \
    done; \
    test -s /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    docker --version; \
    rm -rf /tmp/docker /tmp/docker.tgz /var/lib/apt/lists/*

# 非 root 运行
RUN useradd -r -u 10001 -g 0 appuser && chown -R appuser:0 /app
USER 10001

EXPOSE 5050
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:5050/health').status==200 else sys.exit(1)"

CMD ["python", "-u", "/app/server.py"]
