FROM python:3.12-slim

WORKDIR /app

COPY onkyo_web.py onkyo_cast.py /app/

# config.json is written here (mount /config to keep it across container rebuilds)
ENV ONKYO_CONFIG=/config/config.json
RUN mkdir -p /config

ENTRYPOINT ["python", "/app/onkyo_web.py"]
