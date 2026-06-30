FROM python:3.10-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends mosquitto-clients openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY powerMQTT.py .

VOLUME ["/config"]
ENV IDRAC_MQTT_HOST=mosquitto

ENTRYPOINT ["python", "powerMQTT.py"]
CMD ["--config", "/config/servers.json"]
