FROM python:3.13-slim
WORKDIR /app
COPY server.py engine.py ./
COPY public ./public
ENV PORT=3000
EXPOSE 3000
CMD ["python3", "server.py"]
