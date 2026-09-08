from faker import Faker
import psycopg2
import random
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from indexops.contract import CATEGORIES as categories, PRIORITIES as priorities, TEAMS_BY_CATEGORY as teams_by_category

fake = Faker()

# Works from the Windows host (localhost) and from inside a container (PG_HOST=postgres).
conn = psycopg2.connect(host=os.getenv("PG_HOST", "localhost"), port=5432,
                        user="airflow", password="airflow", dbname="airflow")
cur = conn.cursor()

for _ in range(3000):
    category = random.choice(categories)
    cur.execute(
        """INSERT INTO tickets (description, category, priority, assigned_team, status)
           VALUES (%s, %s, %s, %s, %s)""",
        (fake.sentence(nb_words=10), category, random.choice(priorities),
         teams_by_category[category], "open")
    )

conn.commit()
cur.close()
conn.close()
print("Inserted 3000 tickets.")