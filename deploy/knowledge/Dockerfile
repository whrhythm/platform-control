FROM platform-control:0.1.0-ai-projects-20261106.1
USER root
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple fastembed==0.7.3
COPY model/ /opt/knowledge-model/
COPY control_plane/knowledge_store.py control_plane/knowledge_format.py /app/control_plane/
ENV KNOWLEDGE_MODEL_PATH=/opt/knowledge-model HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
USER 10001
CMD ["uvicorn", "control_plane.knowledge_store:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "8"]
