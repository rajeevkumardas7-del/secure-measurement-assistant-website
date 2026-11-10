FROM python:3.12-slim
WORKDIR /app
COPY domain.py server.py ./
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
