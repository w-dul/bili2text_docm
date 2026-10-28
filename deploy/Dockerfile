# 装配层：唯一同时知道「应用」与「文档站」的地方
# 单镜像、双 URL：/ → 应用（8000），/docs/ → 文档站静态产物
# 状态：骨架配置，尚未实际构建验证

FROM node:20-slim AS docs
WORKDIR /build
COPY docs-site/package.json docs-site/package-lock.json ./docs-site/
RUN npm --prefix docs-site ci
COPY PROJECT.md LOG.md SOP.md ./
COPY assets/framework.svg ./assets/framework.svg
COPY showcase ./showcase
COPY docs-site ./docs-site
RUN npm --prefix docs-site run docs:build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY src ./src
COPY main.py ./

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/entrypoint.sh /entrypoint.sh
COPY --from=docs /build/docs-site/.vitepress/dist /usr/share/nginx/html/docs
RUN chmod +x /entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
