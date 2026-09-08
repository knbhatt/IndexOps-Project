import os
import sys
from sentence_transformers import SentenceTransformer
from opensearchpy import OpenSearch

# This script is meant to run INSIDE the airflow-scheduler container
# (torch is baked into that image), e.g.:
#   docker exec indexops-airflow-scheduler python /opt/airflow/data/index_knowledge_base.py
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PROJECT_ROOT)
from indexops.contract import EMBEDDING_MODEL, EMBEDDING_DIM, KNOWLEDGE_BASE_INDEX

model = SentenceTransformer(EMBEDDING_MODEL)
client = OpenSearch(hosts=[{"host": os.getenv("OS_HOST", "opensearch"), "port": 9200}], use_ssl=False)

index_name = KNOWLEDGE_BASE_INDEX
if not client.indices.exists(index=index_name):
    client.indices.create(index=index_name, body={
        "mappings": {
            "properties": {
                "title": {"type": "text"},
                "content": {"type": "text"},
                "embedding": {"type": "knn_vector", "dimension": EMBEDDING_DIM}
            }
        },
        "settings": {"index.knn": True}
    })

kb_folder = os.path.join(PROJECT_ROOT, "knowledge_base")
for filename in os.listdir(kb_folder):
    filepath = os.path.join(kb_folder, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    embedding = model.encode(content).tolist()
    client.index(index=index_name, id=filename, body={
        "title": filename,
        "content": content,
        "embedding": embedding
    })
    print(f"Indexed: {filename}")

client.indices.refresh(index=index_name)
print(f"Done. {index_name} count = {client.count(index=index_name)['count']}")