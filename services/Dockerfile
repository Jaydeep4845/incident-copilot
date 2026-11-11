FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sut ./sut
ENV PYTHONUNBUFFERED=1
# The service to run is chosen by the compose file, e.g. `uvicorn sut.orders:app`
