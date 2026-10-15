# Airflow with the BI pipeline's dependencies in an isolated virtualenv
# (Airflow 2.x pins SQLAlchemy 1.4; the pipeline uses SQLAlchemy 2 + pandas 2).
FROM apache/airflow:2.10.5-python3.12
COPY requirements.txt /tmp/bi-requirements.txt
RUN python -m venv /opt/bi-venv \
 && /opt/bi-venv/bin/pip install --no-cache-dir -r /tmp/bi-requirements.txt
