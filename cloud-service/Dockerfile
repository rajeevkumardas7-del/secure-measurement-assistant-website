FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY domain.py server.py ./
COPY estimate.py pdf_reader.py trained_reader.py secure-reader-model.json reader-catalog.json ./
COPY test_framework.py test_api.py test_pdf.py ./
RUN python -c "from estimate import ocr_reader_status; ready, detail = ocr_reader_status(); assert ready, detail"
RUN python -m unittest discover -v
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
