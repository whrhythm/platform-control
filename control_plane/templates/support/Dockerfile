FROM harbor.k3s.local/codechat-base/support-runtime@sha256:bb6e9b96ff6fafce390000c390ad4987c9b4b3e17ec3ca9c8261848c7fbe8c29
WORKDIR /app/support
COPY --chown=10001:10001 app.py index.html settings.json ./
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
