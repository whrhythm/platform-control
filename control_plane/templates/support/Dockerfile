FROM harbor.k3s.local/codechat-base/support-runtime@sha256:3e98e2062b10423e32f99adc66fb8366c089fd7b10fd0b7bff5bcdeb1ec689ef
WORKDIR /app/support
COPY --chown=10001:10001 app.py index.html settings.json ./
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
