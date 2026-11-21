# 앞단 서버와 저작 서버가 같이 쓰는 이미지. 파이썬 표준 라이브러리만 쓰고, 저작 서버를 위해 adb가 들어 있다.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends adb && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY manual/ manual/
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 FRONT_HOST=0.0.0.0 AUTHOR_HOST=0.0.0.0 MANUALS_DIR=/manuals RUNS_DIR=/runs
CMD ["python", "manual/front.py"]
