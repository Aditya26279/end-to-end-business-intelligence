FROM python:3.12-slim
WORKDIR /opt/bi
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home bi
COPY bi ./bi
COPY sql ./sql
COPY run_pipeline.py .
USER bi
EXPOSE 8000
# BI_DASHBOARD_PASSWORD must be set: serve refuses to listen on 0.0.0.0 without authentication
CMD ["python", "run_pipeline.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
