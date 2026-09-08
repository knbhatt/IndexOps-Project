FROM apache/airflow:2.9.0

RUN pip install --no-cache-dir \
    "numpy<2" \
    sentence-transformers \
    opensearch-py \
    torch --extra-index-url https://download.pytorch.org/whl/cpu