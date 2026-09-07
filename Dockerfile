# Hugging Face Spaces（Docker SDK）部署用
# HF 约定：容器必须监听 7860 端口并以非 root 运行
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# 先装依赖再拷代码，利用 Docker 层缓存
COPY requirements.txt requirements-hf.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-hf.txt

COPY --chown=user:user . .

# HF Spaces 固定 7860；本地容器可用 PORT 覆盖
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
